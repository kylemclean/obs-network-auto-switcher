import obspython as obs
import threading
import socket
import json
import time
import queue
import select
import os
from dataclasses import dataclass

script_settings = None
enabled = False
switch_interval = 1.0
scene_managers = {}
command_queue = queue.Queue()
result_queue = queue.Queue()
network_thread = None
pipe_read_fd, pipe_write_fd = -1, -1


@dataclass
class DisconnectCommand:
    scene_name: str


@dataclass
class ConnectCommand:
    scene_name: str
    address: str


@dataclass
class LogResult:
    message: str


@dataclass
class ScoreResult:
    scene_name: str
    score: float


@dataclass
class StatusResult:
    scene_name: str
    connected: bool


def log(message):
    if result_queue:
        result_queue.put(LogResult(message))


def on_obs_frontend_event(event):
    if event == obs.OBS_FRONTEND_EVENT_FINISHED_LOADING:
        script_update(script_settings)

        # Needed, because otherwise having an event callback when the scene is set
        # in a timer causes OBS to crash.
        # https://github.com/obsproject/obs-studio/issues/7516
        # https://stackoverflow.com/questions/73142444/obs-crashes-when-set-current-scene-function-called-within-a-timer-callback-pyth
        # https://obsproject.com/forum/threads/obs-crashes-when-switching-scene-during-a-timer_add-python-scripting.157999/

        obs.obs_frontend_remove_event_callback(on_obs_frontend_event)


def get_current_obs_scenes():
    scenes = obs.obs_frontend_get_scenes()
    if not scenes:
        return []
    scene_names = [obs.obs_source_get_name(scene) for scene in scenes]
    obs.source_list_release(scenes)
    return scene_names


class SceneManager:
    """Manages the state for a single scene. Does not perform network I/O."""

    def __init__(self, scene_name, settings):
        self.scene_name = scene_name
        self.lock = threading.Lock()
        self.is_managed = False
        self.server_address = ""
        self.default_score = -1.0
        self.current_score = -1.0
        self.is_connected = False
        self.update(settings)

    def update(self, settings):
        with self.lock:
            managed_key = f"{self.scene_name}_managed"
            address_key = f"{self.scene_name}_server_address"
            score_key = f"{self.scene_name}_default_score"
            was_managed = self.is_managed
            old_address = self.server_address
            self.is_managed = obs.obs_data_get_bool(settings, managed_key)
            self.server_address = obs.obs_data_get_string(settings, address_key)
            self.default_score = obs.obs_data_get_double(settings, score_key)
            self.current_score = self.default_score

            config_changed = (self.is_managed != was_managed) or (
                self.server_address != old_address
            )
            if config_changed:
                self.is_connected = False
                if was_managed:
                    command_queue.put(DisconnectCommand(self.scene_name))

    def set_score(self, score):
        with self.lock:
            self.current_score = score

    def get_score(self):
        with self.lock:
            if not self.is_managed:
                return -float("inf")
            return self.current_score


def network_loop(pipe_read):
    """The single, dedicated thread for all networking."""
    sockets_by_scene_name = {}  # scene_name -> socket
    scene_names_by_socket = {}  # socket -> scene_name
    buffers_by_socket = {}  # socket -> buffer
    inputs = [pipe_read]
    connecting_sockets = []  # Sockets that are in the process of connecting

    while True:
        try:
            readable, writable, exceptional = select.select(
                inputs, connecting_sockets, inputs, 0.5
            )

            for sock in readable:
                if sock == pipe_read:
                    os.read(pipe_read, 1)
                    for s in sockets_by_scene_name.values():
                        s.close()
                    return

                scene_name = scene_names_by_socket.get(sock)
                if not scene_name:
                    continue

                try:
                    data = sock.recv(1024)
                    if not data:
                        raise ConnectionResetError

                    buffers_by_socket[sock] += data.decode("utf-8")
                    while "\n" in buffers_by_socket[sock]:
                        line, buffers_by_socket[sock] = buffers_by_socket[sock].split(
                            "\n", 1
                        )
                        try:
                            message = json.loads(line)
                            if "score" in message:
                                result_queue.put(
                                    ScoreResult(
                                        scene_name=scene_name,
                                        score=float(message["score"]),
                                    )
                                )
                        except (json.JSONDecodeError, ValueError):
                            log(f"[{scene_name}] Invalid JSON")
                except Exception:
                    if sock in connecting_sockets:
                        log(f"[{scene_name}] Failed to connect.")
                        connecting_sockets.remove(sock)
                    else:
                        log(f"[{scene_name}] Disconnected.")
                    result_queue.put(
                        StatusResult(scene_name=scene_name, connected=False)
                    )
                    sock.close()
                    inputs.remove(sock)
                    buffers_by_socket.pop(sock, None)
                    sockets_by_scene_name.pop(scene_name, None)
                    scene_names_by_socket.pop(sock, None)

            for sock in exceptional:
                scene_name = scene_names_by_socket.get(sock)
                if not scene_name:
                    continue
                if sock in connecting_sockets:
                    log(f"[{scene_name}] Failed to connect.")
                    connecting_sockets.remove(sock)
                else:
                    log(f"[{scene_name}] Socket exception.")
                result_queue.put(StatusResult(scene_name=scene_name, connected=False))
                sock.close()
                inputs.remove(sock)
                buffers_by_socket.pop(sock, None)
                sockets_by_scene_name.pop(scene_name, None)
                scene_names_by_socket.pop(sock, None)

            while not command_queue.empty():
                cmd = command_queue.get_nowait()

                if isinstance(cmd, DisconnectCommand):
                    scene_name = cmd.scene_name
                    if scene_name in sockets_by_scene_name:
                        sock_to_close = sockets_by_scene_name[scene_name]
                        sock_to_close.close()
                        inputs.remove(sock_to_close)
                        del sockets_by_scene_name[scene_name]
                        del scene_names_by_socket[sock_to_close]
                        if sock_to_close in connecting_sockets:
                            connecting_sockets.remove(sock_to_close)
                    continue

                if isinstance(cmd, ConnectCommand):
                    scene_name = cmd.scene_name
                    address = cmd.address

                    if scene_name in sockets_by_scene_name:
                        sock_to_close = sockets_by_scene_name[scene_name]
                        sock_to_close.close()
                        inputs.remove(sock_to_close)
                        del sockets_by_scene_name[scene_name]
                        del scene_names_by_socket[sock_to_close]
                        if sock_to_close in connecting_sockets:
                            connecting_sockets.remove(sock_to_close)

                    log(f"[{scene_name}] Connecting to {address}...")

                    try:
                        host, port_str = address.split(":")
                        port = int(port_str)
                        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                        sock.setblocking(False)
                        # Store the socket temporarily until connection is established
                        sockets_by_scene_name[scene_name] = sock
                        scene_names_by_socket[sock] = scene_name
                        buffers_by_socket[sock] = ""
                        # Add to inputs for select to monitor writability
                        inputs.append(sock)
                        sock.connect_ex((host, port))
                        connecting_sockets.append(sock)
                    except Exception as e:
                        log(f"[{scene_name}] Failed to connect: {e}")
                        result_queue.put(
                            StatusResult(scene_name=scene_name, connected=False)
                        )

            for sock in writable:
                scene_name = scene_names_by_socket.get(sock)
                if not scene_name:
                    continue

                # Remove from connecting_sockets as we've processed its writability
                if sock in connecting_sockets:
                    connecting_sockets.remove(sock)

                err = sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
                if err == 0:
                    log(
                        f"[{scene_name}] Successfully connected to {sock.getpeername()}"
                    )
                    result_queue.put(
                        StatusResult(scene_name=scene_name, connected=True)
                    )
                else:
                    log(f"[{scene_name}] Failed to connect: {os.strerror(err)}")
                    result_queue.put(
                        StatusResult(scene_name=scene_name, connected=False)
                    )
                    sock.close()
                    inputs.remove(sock)
                    buffers_by_socket.pop(sock, None)
                    sockets_by_scene_name.pop(scene_name, None)
                    scene_names_by_socket.pop(sock, None)

        except Exception as e:
            log(f"Network thread error: {e}")
            time.sleep(1)


def result_processor_tick():
    """Processes results from the network thread in the main OBS thread."""
    while not result_queue.empty():
        try:
            res = result_queue.get_nowait()

            if isinstance(res, LogResult):
                obs.script_log(obs.LOG_INFO, res.message)
                continue

            if isinstance(res, ScoreResult):
                if res.scene_name in scene_managers:
                    scene_managers[res.scene_name].set_score(res.score)
                continue

            if isinstance(res, StatusResult):
                if res.scene_name in scene_managers:
                    scene_managers[res.scene_name].is_connected = res.connected
                continue

        except queue.Empty:
            break


def connection_watchdog_tick():
    """Periodically checks if managed scenes need to be connected."""
    for scene_name, manager in scene_managers.items():
        with manager.lock:
            if (
                not manager.is_managed
                or manager.is_connected
                or not manager.server_address
            ):
                continue

            command_queue.put(
                ConnectCommand(scene_name=scene_name, address=manager.server_address)
            )


def setup_scene_managers():
    current_scene_names = get_current_obs_scenes()

    for scene_name in current_scene_names:
        if scene_name in scene_managers:
            scene_managers[scene_name].update(script_settings)
            continue
        scene_managers[scene_name] = SceneManager(scene_name, script_settings)

    for scene_name in list(scene_managers.keys()):
        if scene_name not in current_scene_names:
            command_queue.put(DisconnectCommand(scene_name))
            del scene_managers[scene_name]


def script_description():
    return "Switches scenes based on scores from a TCP server."


def script_properties():
    props = obs.obs_properties_create()
    obs.obs_properties_add_bool(props, "enabled", "Enable Auto Switching")
    obs.obs_properties_add_float(
        props, "switch_interval", "Scene Switch Interval (seconds)", 0.1, 3600, 0.1
    )
    scene_names = get_current_obs_scenes()
    if not scene_names:
        return props

    for scene_name in scene_names:
        group = obs.obs_properties_create()
        obs.obs_properties_add_bool(
            group, f"{scene_name}_managed", "Enable for this scene"
        )
        obs.obs_properties_add_text(
            group,
            f"{scene_name}_server_address",
            "Server Address (host:port)",
            obs.OBS_TEXT_DEFAULT,
        )
        obs.obs_properties_add_float(
            group,
            f"{scene_name}_default_score",
            "Default Score",
            -1000000,
            1000000,
            0.1,
        )
        obs.obs_properties_add_group(
            props,
            f"group_{scene_name}",
            f"Scene: {scene_name}",
            obs.OBS_GROUP_NORMAL,
            group,
        )
    return props


def script_defaults(settings):
    obs.obs_data_set_default_bool(settings, "enabled", True)
    obs.obs_data_set_default_double(settings, "switch_interval", 1.0)
    scene_names = get_current_obs_scenes()
    if not scene_names:
        return

    for scene_name in scene_names:
        obs.obs_data_set_default_bool(settings, f"{scene_name}_managed", False)
        obs.obs_data_set_default_double(settings, f"{scene_name}_default_score", -1.0)


def script_load(settings):
    global network_thread, pipe_read_fd, pipe_write_fd, script_settings
    pipe_read_fd, pipe_write_fd = os.pipe()
    network_thread = threading.Thread(target=network_loop, args=(pipe_read_fd,))
    network_thread.start()
    script_settings = settings
    obs.timer_add(scene_switcher_tick, 1000)
    obs.timer_add(result_processor_tick, 100)
    obs.timer_add(connection_watchdog_tick, 5000)
    obs.obs_frontend_add_event_callback(on_obs_frontend_event)

    log("Network Auto Switcher loaded")

    script_update(settings)


def script_update(settings):
    global enabled, switch_interval

    enabled = obs.obs_data_get_bool(settings, "enabled")
    new_interval = obs.obs_data_get_double(settings, "switch_interval")
    if new_interval != switch_interval:
        switch_interval = new_interval
        obs.timer_remove(scene_switcher_tick)
        obs.timer_add(scene_switcher_tick, int(switch_interval * 1000))

    setup_scene_managers()

    log("Network Auto Switcher settings updated")


def script_unload():
    global pipe_read_fd, pipe_write_fd
    if pipe_write_fd != -1:
        os.write(pipe_write_fd, b"x")
    if network_thread:
        network_thread.join()
    if pipe_read_fd != -1:
        os.close(pipe_read_fd)
    if pipe_write_fd != -1:
        os.close(pipe_write_fd)
    pipe_read_fd, pipe_write_fd = -1, -1
    obs.timer_remove(scene_switcher_tick)
    obs.timer_remove(result_processor_tick)
    obs.timer_remove(connection_watchdog_tick)
    obs.obs_frontend_remove_event_callback(on_obs_frontend_event)
    scene_managers.clear()

    log("Network Auto Switcher unloaded")


def scene_switcher_tick():
    if not enabled:
        return

    highest_score = -float("inf")
    best_scene_name = None

    # Find the best scene by name
    for scene_name, manager in scene_managers.items():
        score = manager.get_score()
        if score > highest_score:
            highest_score = score
            best_scene_name = scene_name

    if not best_scene_name:
        return

    # Get the current scene's name in a safe, isolated way
    current_scene_source = obs.obs_frontend_get_current_scene()
    current_scene_name = None
    if current_scene_source:
        current_scene_name = obs.obs_source_get_name(current_scene_source)
        # Immediately release the handle after we're done with it
        obs.obs_source_release(current_scene_source)

    # If the best scene is not the current one, switch to it
    if current_scene_name == best_scene_name:
        return

    best_scene_source = obs.obs_get_source_by_name(best_scene_name)
    if not best_scene_source:
        return

    obs.obs_frontend_set_current_scene(best_scene_source)
    log(f"Switched to scene '{best_scene_name}' with score {highest_score}")
    obs.obs_source_release(best_scene_source)
