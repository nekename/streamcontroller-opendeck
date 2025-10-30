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
                            action.set_media(image=Image.open(BytesIO(data)))
                        else:
                            action.set_media(media_path=greyscale_image)
                    else:
                        for context, action in actions.items():
                            if context[0] == data["device"]:
                                action.set_media(media_path=greyscale_image)

        except Exception as e:
            log.warning(e)

        for action in actions.values():
            action.set_media(media_path=greyscale_image)

        sleep(5)
        global registered_devices
        registered_devices = []


class PluginTemplate(PluginBase):
    def __init__(self):
        super().__init__()

        # Support both key (button) and dial actions
        self.action_holder = ActionHolder(
            plugin_base=self,
            action_base=OpenDeckButton,
            action_id_suffix="OpenDeckButton",
            action_name="OpenDeck Button",
            action_support={
                Input.Key: ActionInputSupport.SUPPORTED,
                Input.Dial: ActionInputSupport.SUPPORTED,
            },
        )
        self.add_action_holder(self.action_holder)

        self.register(
            plugin_version="1.0.0",
            plugin_name="OpenDeck",
            github_repo="https://github.com/nekename/opendeck-streamcontroller",
            app_version="1.0.0-alpha",
        )

        global greyscale_image
        greyscale_image = path.join(self.PATH, "greyscale.png")

        Thread(target=connect_to_opendeck).start()


class OpenDeckButton(ActionBase):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.context_store = None

    def context(self) -> (str, str, int):
        # Determine if this is a dial or key input

        controller_type = (
            "Encoder" if self.get_input().identifier.input_type == "dials" else "Keypad"
        )

        new = (
            "sd-" + self.deck_controller.deck.get_serial_number(),
            self.page.get_name(),
            controller_type,
            self.get_input().index
            if controller_type == "Keypad"
            else self.get_input().identifier.index,
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
        ## backward compatibility
        if event == Input.Key.Events.DOWN:
            self.on_key_down()
        elif event == Input.Key.Events.UP:
            self.on_key_up()
        elif event == Input.Dial.Events.DOWN:
            self.on_encoder_down()
        elif event == Input.Dial.Events.UP:
            self.on_encoder_up()
        elif event == Input.Dial.Events.TURN_CW:
            self.on_encoder_turn_cw()
        elif event == Input.Dial.Events.TURN_CCW:
            self.on_encoder_turn_ccw()

    def on_ready(self) -> None:
        self.set_media(media_path=greyscale_image)
        context = self.context()
        send_to_opendeck(
            json.dumps(
                {
                    "event": "rerenderImages",
                    "payload": context[0],
                }
            )
        )

    def on_key(self, direction) -> None:
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
        self.on_key("keyDown")

    def on_key_up(self) -> None:
        self.on_key("keyUp")

    # Add dial-specific methods
    def on_encoder_change(self, ticks) -> None:
        """Handle dial rotation events"""
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
        """Handle dial press down event"""
        context = self.context()
        send_to_opendeck(
            json.dumps(
                {
                    "event": "encoderDown",
                    "payload": {"device": context[0], "position": context[3]},
                }
            )
        )

    def on_encoder_up(self) -> None:
        """Handle dial press up event"""
        context = self.context()
        send_to_opendeck(
            json.dumps(
                {
                    "event": "encoderUp",
                    "payload": {"device": context[0], "position": context[3]},
                }
            )
        )

    # Specialized method for dial rotation that can be triggered by the framework
    def on_encoder_turn_cw(self) -> None:
        """Handle clockwise dial rotation"""
        self.on_encoder_change(1)

    def on_encoder_turn_ccw(self) -> None:
        """Handle counter-clockwise dial rotation"""
        self.on_encoder_change(-1)
