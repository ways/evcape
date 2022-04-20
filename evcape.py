#!/usr/bin/env python3

import argparse
import collections
import errno
import functools
import logging
import selectors

import evdev
import pyudev

logger = logging.getLogger("evcape")

DEFAULT_RULES = [
    "press:leftctrl,release:leftctrl=press:esc,release:esc",
    "press:capslock,release:capslock=press:esc,release:esc",
]
DEFAULT_TIMEOUT = 1000

KEY_EVENT_VALUE_TO_ACTION = {
    0: "release",
    1: "press",
}
ACTION_TO_KEY_EVENT_VALUE = {
    value: key for key, value in KEY_EVENT_VALUE_TO_ACTION.items()
}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("rules", nargs="*", metavar="rule", default=DEFAULT_RULES)
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)

    timeout = args.timeout / 1000
    logger.info(f"using timeout {args.timeout}ms")

    for s in args.rules:
        logger.info(f"adding rule {s!r}")
    rules = [Rule.from_string(s) for s in args.rules]
    assert len(rules) > 0
    rules_by_last_event = {}
    for rule in rules:
        key = rule.patterns[-1]
        rules_by_last_event.setdefault(key, []).append(rule)

    # tell the uinput device about the exact keys used in rule actions since
    # the default value causes events not to propagate to the session somehow;
    # see also https://gitlab.gnome.org/GNOME/mutter/-/issues/1869
    keys = sorted({code for rule in rules for _, code in rule.actions})
    uinput = evdev.UInput(
        events={evdev.ecodes.EV_KEY: keys},
        name="evcape",
    )
    if uinput.device is None:
        raise RuntimeError(
            f"could not access evdev device for uinput device {uinput.name!r}"
        )
    logger.info(f"created uinput device {uinput.device.path}")

    keyboard_monitor = KeyboardMonitor(ignored_devices=[uinput.device.path])

    # the buffer is as long as the longest sequence in rules
    window_size = max(len(rule.patterns) for rule in rules)
    buffer = collections.deque(maxlen=window_size)

    # put keypresses into a buffer and try to match rules
    previous_event_timestamp = 0.0
    with uinput, keyboard_monitor:
        for event in keyboard_monitor:
            lookup_key = (event.value, event.code)
            buffer.append(lookup_key)
            ts_diff = event.timestamp() - previous_event_timestamp
            previous_event_timestamp = event.timestamp()
            if ts_diff >= timeout:
                continue
            possibly_matching_rules = rules_by_last_event.get(lookup_key)
            if not possibly_matching_rules:
                continue
            for rule in possibly_matching_rules:
                offset = -len(rule.patterns)
                buffer_slice = list(buffer)[offset:]
                if rule.patterns != buffer_slice:
                    continue
                for value, code in rule.actions:
                    uinput.write(evdev.ecodes.EV_KEY, code, value)
                uinput.syn()


class KeyboardMonitor:
    def __init__(self, ignored_devices):
        self.udev_context = pyudev.Context()
        self.selector = selectors.DefaultSelector()
        self.ignored_devices = ignored_devices
        self.input_devices_by_name = set()
        self.start_udev_monitor()
        self.add_existing_keyboards()

    def start_udev_monitor(self):
        """
        Start an udev monitor to detect hotplug events.

        This detects when external keyboards are (dis)connected.
        """
        monitor = pyudev.Monitor.from_netlink(self.udev_context)
        monitor.filter_by(subsystem="input")
        monitor.start()
        self.selector.register(monitor, events=selectors.EVENT_READ, data="udev")

    def add_existing_keyboards(self):
        enumerator = self.udev_context.list_devices()
        enumerator.match_subsystem("input")
        enumerator.match_property("ID_INPUT_KEYBOARD", "1")
        for device in enumerator:
            device_name = udev_keyboard_device_name(device)
            if device_name is not None:
                self.add_keyboard(device_name)

    def add_keyboard(self, device_name):
        if device_name in self.ignored_devices:
            return
        try:
            input_device = evdev.InputDevice(device_name)
        except OSError as exc:
            logger.warning(f"could not create input device for {device_name}: {exc}")
            return
        self.selector.register(
            input_device, events=selectors.EVENT_READ, data="keyboard"
        )
        logger.info("monitoring {0.path} ({0.name})".format(input_device))

    def remove_keyboard(self, device_name):
        for selector_key in self.selector.get_map().values():
            input_device = selector_key.fileobj
            if not isinstance(input_device, evdev.InputDevice):
                continue
            if input_device.path != device_name:
                continue
            self.selector.unregister(input_device)
            logger.info("no longer monitoring {0.path} ({0.name})".format(input_device))
            break

    def __iter__(self):
        def read_forever_from_selector():
            while True:
                for selector_key, mask in self.selector.select():
                    yield selector_key

        for selector_key in read_forever_from_selector():
            if selector_key.data == "keyboard":  # keyboard event
                input_device = selector_key.fileobj
                for event in read_input_device_events(input_device):
                    if event.type != evdev.ecodes.EV_KEY:
                        continue
                    if event.value not in KEY_EVENT_VALUE_TO_ACTION:
                        continue  # e.g. key repeat
                    yield event
            elif selector_key.data == "udev":  # hotplug event
                poll_monitor = functools.partial(selector_key.fileobj.poll, timeout=0)
                for device in iter(poll_monitor, None):
                    device_name = udev_keyboard_device_name(device)
                    if device_name is None:
                        continue
                    if device.action == "add":
                        self.add_keyboard(device_name)
                    elif device.action == "remove":
                        self.remove_keyboard(device_name)
            else:
                assert False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

    def close(self):
        for selector_key in list(self.selector.get_map().values()):
            self.selector.unregister(selector_key.fileobj)
        self.selector.close()


def read_input_device_events(input_device):
    try:
        yield from input_device.read()
    except OSError as exc:
        if exc.errno == errno.ENODEV:
            pass  # Device has disappeared.
        else:
            raise


def udev_keyboard_device_name(device):
    if device.properties.get("ID_INPUT_KEYBOARD") != "1":
        return None  # This is not a keyboard.
    try:
        return device.properties["DEVNAME"]
    except KeyError:
        return None


_Rule = collections.namedtuple("Rule", ["patterns", "actions"])


class Rule(_Rule):
    @classmethod
    def from_string(cls, s):
        """
        Create a rule from a string.

        Sample input:

          press:leftctrl,release:leftctrl=press:esc,release:esc

        Sample output:

          Rule(
              patterns=[(1, 29), (0, 29)],
              actions=[(1, 1), (0, 1)])

        Key codes:

          KEY_LEFTCTRL == 29
          KEY_ESC == 1

        Key values:

          press == 1
          release == 0
        """
        patterns, _, actions = s.partition("=")
        return cls(
            patterns=cls.parse_sequence(patterns), actions=cls.parse_sequence(actions)
        )

    @staticmethod
    def parse_sequence(s):
        out = []
        for chunk in s.split(","):
            action, _, key = chunk.partition(":")
            value = ACTION_TO_KEY_EVENT_VALUE[action]
            code = getattr(evdev.ecodes, f"KEY_{key.upper()}")
            out.append((value, code))
        return out


if __name__ == "__main__":
    main()
