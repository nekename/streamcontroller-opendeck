from src.backend.PluginManager.ActionBase import ActionBase
from src.backend.PluginManager.PluginBase import PluginBase
from src.backend.PluginManager.ActionHolder import ActionHolder
from src.backend.PluginManager.ActionInputSupport import ActionInputSupport
from src.backend.DeckManagement.InputIdentifier import Input, InputEvent
from src.Signals import Signals

import globals as gl

import json
from websocket import WebSocket
from threading import Thread

from base64 import b64decode
from PIL import Image
from io import BytesIO

from time import sleep
from os import path
from socket import socket, AF_INET, SOCK_DGRAM
from loguru import logger as log

registered_devices = []
actions = {}
greyscale_image = None

ws = WebSocket()


def send_to_opendeck(data):
    global ws
    try:
        if ws.connected:
            ws.send(data)
    except Exception:
        ws.close()
        ws = WebSocket()


def change_page(controller, *args):
    deck = controller.deck
    id = "sd-" + deck.get_serial_number()
    page = controller.active_page.get_name()

    if id not in registered_devices:
        name = deck.deck_type() + " (StreamController)"
        (rows, columns) = deck.key_layout()
        encoders = deck.dial_count()
        # Stream Deck Neo extras: OpenDeck puts touch points on the Keypad
        # controller after the regular keys, and the info bar on "Infobar"
        try:
            touchpoints = deck.touch_key_count()
        except Exception:
            touchpoints = 0
        try:
            has_screen = deck.screen_image_format().get("size", (0, 0)) != (0, 0)
        except Exception:
            has_screen = False

        send_to_opendeck(
            json.dumps(
                {
                    "event": "registerDevice",
                    "payload": {
                        "id": id,
                        "name": name,
                        "rows": rows,
                        "columns": columns,
                        "encoders": encoders,
                        "touchpoints": touchpoints,
                        "infobars": 1 if has_screen else 0,
                        "type": 0,
                    },
                }
            )
        )
        registered_devices.append(id)
        log.info(f"Registered device {id}")
        sleep(1)

    send_to_opendeck(
        json.dumps(
            {
                "event": "switchProfile",
                "device": id,
                "profile": page,
            }
        )
    )


gl.signal_manager.connect_signal(signal=Signals.ChangePage, callback=change_page)


def connect_to_opendeck() -> None:
    global ws, registered_devices
    # If we connect before StreamController has created its deck manager, the
    # first registration fails part-way through and OpenDeck keeps it alive,
    # so every later reconnect is rejected as a duplicate registration.
    while getattr(gl, "deck_manager", None) is None:
        sleep(0.5)
    while True:
        try:
            # Flatpak `localhost` only resolves internally so we try to use the host local IP
            s = socket(AF_INET, SOCK_DGRAM)
            try:
                s.connect(("8.8.8.8", 80))
                ip = s.getsockname()[0]
            except Exception as e:
                log.error(f"Could not discover local IP: {e}")
                ip = "127.0.0.1"
            finally:
                s.close()
            log.info(f"Connecting to OpenDeck using IP {ip}")

            ws.connect(f"ws://{ip}:57116")
            send_to_opendeck(
                json.dumps(
                    {
                        "event": "registerPlugin",
                        "uuid": "opendeck_alternative_elgato_implementation",
                    }
                )
            )
            log.success("Connected to OpenDeck")
            # Devices "registered" while disconnected (e.g. on a page change during
            # startup) never reached OpenDeck, so register them again now
            registered_devices = []
            for controller in gl.deck_manager.deck_controller:
                change_page(controller)

            while True:
                recv = ws.recv()
                data = json.loads(recv)
                if data["event"] == "setImage":
                    if data.get("position") is not None:
                        controller = next(
                            c
                            for c in gl.deck_manager.deck_controller
                            if "sd-" + c.deck.get_serial_number() == data["device"]
                        )
                        action = actions.get(
                            (
                                data["device"],
                                controller.active_page.get_name(),
                                data["controller"],
                                data["position"],
                            )
                        )
                        if action is None:
                            continue

                        image = data.get("image")
                        if image is not None:
                            encoded = image.split(",", 1)[1]
                            data = b64decode(encoded)
                            action.show_image(Image.open(BytesIO(data)))
                        else:
                            action.show_image(None)
                    else:
                        for context, action in actions.items():
                            if context[0] == data["device"]:
                                action.show_image(None)

        except Exception as e:
            log.warning(e)

        # Close the old connection so OpenDeck drops our registration before we
        # retry; otherwise the retry is rejected as a duplicate registration
        try:
            ws.close()
        except Exception:
            pass
        ws = WebSocket()

        for action in actions.values():
            action.show_image(None)

        sleep(5)
        registered_devices = []


class PluginTemplate(PluginBase):
    def __init__(self):
        super().__init__()

        action_support = {
            Input.Key: ActionInputSupport.SUPPORTED,
            Input.Dial: ActionInputSupport.SUPPORTED,
        }
        # Stream Deck Neo touch points and info bar (only in newer StreamController versions)
        for input_class in ("TouchKey", "Screen"):
            if hasattr(Input, input_class):
                action_support[getattr(Input, input_class)] = ActionInputSupport.SUPPORTED

        self.action_holder = ActionHolder(
            plugin_base=self,
            action_base=OpenDeckButton,
            action_id_suffix="OpenDeckButton",
            action_name="OpenDeck Button",
            action_support=action_support,
        )
        self.add_action_holder(self.action_holder)

        self.register(
            plugin_version="1.1.0",
            plugin_name="OpenDeck",
            github_repo="https://github.com/nekename/opendeck-streamcontroller",
            app_version="1.5.0-beta.11",
        )

        global greyscale_image
        greyscale_image = path.join(self.PATH, "greyscale.png")

        Thread(target=connect_to_opendeck).start()


class OpenDeckButton(ActionBase):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.context_store = None

    def input_type(self) -> str:
        return self.get_input().identifier.input_type

    def context(self) -> (str, str, str, int):
        input_type = self.input_type()
        identifier = self.get_input().identifier
        if input_type == "dials":
            controller, position = "Encoder", identifier.index
        elif input_type == "touch_keys":
            # OpenDeck numbers touch points after the regular keys
            controller = "Keypad"
            position = self.deck_controller.deck.key_count() + identifier.index
        elif input_type == "screens":
            controller, position = "Infobar", 0
        else:
            controller, position = "Keypad", self.get_input().index
        new = (
            "sd-" + self.deck_controller.deck.get_serial_number(),
            self.page.get_name(),
            controller,
            position,
        )
        if new != self.context_store:
            if self.context_store is not None:
                del actions[self.context_store]
                log.info(f"Removed action {self.context_store}")
            actions[new] = self
            log.info(f"Created action {new}")
            self.context_store = new
        return new

    def event_callback(self, event: InputEvent, data: dict = None):
        if event == Input.Key.Events.DOWN:
            self.on_key_down()
        elif event == Input.Key.Events.UP:
            self.on_key_up()
        elif event == Input.Dial.Events.DOWN:
            self.on_encoder_down()
        elif event == Input.Dial.Events.UP:
            self.on_encoder_up()
        elif event == Input.Dial.Events.TURN_CW:
            self.on_encoder_change(1)
        elif event == Input.Dial.Events.TURN_CCW:
            self.on_encoder_change(-1)

    def show_image(self, image) -> None:
        """Show an image from OpenDeck, or the placeholder if image is None."""
        if self.input_type() == "touch_keys":
            # Touch points can only show a colour: use the image's average, as OpenDeck does
            if image is None:
                self.set_background_color([0, 0, 0, 255])
            else:
                r, g, b = image.convert("RGB").resize((1, 1), Image.BOX).getpixel((0, 0))
                self.set_background_color([r, g, b, 255])
        elif image is None:
            self.set_media(media_path=greyscale_image)
        else:
            self.set_media(image=image)

    def on_ready(self) -> None:
        if self.input_type() not in ("keys", "dials", "touch_keys", "screens"):
            return
        self.show_image(None)
        context = self.context()
        send_to_opendeck(
            json.dumps(
                {
                    "event": "rerenderImages",
                    "payload": context[0],
                }
            )
        )

    def on_press(self, direction) -> None:
        context = self.context()
        send_to_opendeck(
            json.dumps(
                {
                    "event": direction,
                    "payload": {"device": context[0], "position": context[3]},
                }
            )
        )

    def on_key_down(self) -> None:
        self.on_press("keyDown")

    def on_key_up(self) -> None:
        self.on_press("keyUp")

    def on_encoder_change(self, ticks) -> None:
        context = self.context()
        send_to_opendeck(
            json.dumps(
                {
                    "event": "encoderChange",
                    "payload": {
                        "device": context[0],
                        "position": context[3],
                        "ticks": ticks,
                    },
                }
            )
        )

    def on_encoder_down(self) -> None:
        self.on_press("encoderDown")

    def on_encoder_up(self) -> None:
        self.on_press("encoderUp")
