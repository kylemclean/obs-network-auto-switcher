import obspython as obs
import threading
import socket
import json
import time
import queue
import select
import os

enabled = False
switch_interval = 1.0
scene_managers = {}
command_queue = queue.Queue()
result_queue = queue.Queue()
network_thread = None
pipe_read_fd, pipe_write_fd = -1, -1


def log(message):
    if result_queue:
        result_queue.put({"type": "log", "message": message})


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
                    command_queue.put(
                        {"command": "disconnect", "scene_name": self.scene_name}
                    )

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
    sockets = {}
    buffers = {}
    inputs = [pipe_read]

    while True:
        try:
            readable, _, exceptional = select.select(inputs, [], inputs, 0.5)

            for sock in readable:
                if sock == pipe_read:
                    os.read(pipe_read, 1)
                    for s in sockets.values():
                        s.close()
                    return

                scene_name = next(
                    (name for name, s in sockets.items() if s == sock), None
                )
                if not scene_name:
                    continue

                try:
                    data = sock.recv(1024)
                    if data:
                        buffers[sock] += data.decode("utf-8")
                        while "\n" in buffers[sock]:
                            line, buffers[sock] = buffers[sock].split("\n", 1)
                            try:
                                message = json.loads(line)
                                if "score" in message:
                                    result_queue.put(
                                        {
                                            "type": "score",
                                            "scene_name": scene_name,
                                            "score": float(message["score"]),
                                        }
                                    )
                            except (json.JSONDecodeError, ValueError):
                                log(f"[{scene_name}] Invalid JSON")
                    else:
                        raise ConnectionResetError
                except Exception:
                    log(f"[{scene_name}] Disconnected.")
                    result_queue.put(
                        {"type": "status", "scene_name": scene_name, "connected": False}
                    )
                    sock.close()
                    inputs.remove(sock)
                    if sock in buffers:
                        del buffers[sock]
                    if scene_name in sockets:
                        del sockets[scene_name]

            for sock in exceptional:
                scene_name = next(
                    (name for name, s in sockets.items() if s == sock), None
                )
                if not scene_name:
                    continue
                log(f"[{scene_name}] Socket exception.")
                result_queue.put(
                    {"type": "status", "scene_name": scene_name, "connected": False}
                )
                sock.close()
                inputs.remove(sock)
                if sock in buffers:
                    del buffers[sock]
                if scene_name in sockets:
                    del sockets[scene_name]

            while not command_queue.empty():
                cmd = command_queue.get_nowait()
                scene_name = cmd.get("scene_name")
                if scene_name in sockets:
                    sockets[scene_name].close()
                    inputs.remove(sockets[scene_name])
                    del sockets[scene_name]

                if cmd["command"] == "connect":
                    try:
                        host, port_str = cmd["address"].split(":")
                        port = int(port_str)
                        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                        sock.setblocking(False)
                        sock.connect_ex((host, port))
                        sockets[scene_name] = sock
                        buffers[sock] = ""
                        inputs.append(sock)
                        result_queue.put(
                            {
                                "type": "status",
                                "scene_name": scene_name,
                                "connected": True,
                            }
                        )
                        log(f"[{scene_name}] Connection attempt initiated.")
                    except Exception as e:
                        log(f"[{scene_name}] Connection command failed: {e}")
                        result_queue.put(
                            {
                                "type": "status",
                                "scene_name": scene_name,
                                "connected": False,
                            }
                        )

        except Exception as e:
            log(f"Network thread error: {e}")
            time.sleep(1)


def result_processor_tick():
    """Processes results from the network thread in the main OBS thread."""
    while not result_queue.empty():
        try:
            res = result_queue.get_nowait()
            res_type = res.get("type")

            if res_type == "log":
                obs.script_log(obs.LOG_INFO, res["message"])
                continue

            scene_name = res.get("scene_name")
            if not scene_name or scene_name not in scene_managers:
                continue

            if res_type == "score":
                scene_managers[scene_name].set_score(res["score"])
            elif res_type == "status":
                scene_managers[scene_name].is_connected = res["connected"]
        except queue.Empty:
            break


def connection_watchdog_tick():
    """Periodically checks if managed scenes need to be connected."""
    for scene_name, manager in scene_managers.items():
        with manager.lock:
            if (
                manager.is_managed
                and not manager.is_connected
                and manager.server_address
            ):
                log(f"Watchdog: Reconnecting {scene_name}")
                command_queue.put(
                    {
                        "command": "connect",
                        "scene_name": scene_name,
                        "address": manager.server_address,
                    }
                )


def script_description():
    return "Switches scenes based on scores from a TCP server."


def script_properties():
    props = obs.obs_properties_create()
    obs.obs_properties_add_bool(props, "enabled", "Enable Auto Switching")
    obs.obs_properties_add_float(
        props, "switch_interval", "Scene Switch Interval (seconds)", 0.1, 3600, 0.1
    )
    scenes = obs.obs_frontend_get_scenes()
    if scenes:
        for scene in scenes:
            scene_name = obs.obs_source_get_name(scene)
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
            score_label = obs.obs_properties_add_text(
                group, f"{scene_name}_current_score", "Current Score", obs.OBS_TEXT_INFO
            )
            manager = scene_managers.get(scene_name)
            obs.obs_property_set_description(
                score_label, str(manager.get_score()) if manager else "N/A"
            )
            obs.obs_properties_add_group(
                props,
                f"group_{scene_name}",
                f"Scene: {scene_name}",
                obs.OBS_GROUP_NORMAL,
                group,
            )
        obs.source_list_release(scenes)
    return props


def script_defaults(settings):
    obs.obs_data_set_default_bool(settings, "enabled", True)
    obs.obs_data_set_default_double(settings, "switch_interval", 1.0)
    scenes = obs.obs_frontend_get_scenes()
    if scenes:
        for scene in scenes:
            scene_name = obs.obs_source_get_name(scene)
            obs.obs_data_set_default_bool(settings, f"{scene_name}_managed", False)
            obs.obs_data_set_default_double(
                settings, f"{scene_name}_default_score", -1.0
            )
        obs.source_list_release(scenes)


def script_load(settings):
    global network_thread, pipe_read_fd, pipe_write_fd
    pipe_read_fd, pipe_write_fd = os.pipe()
    network_thread = threading.Thread(target=network_loop, args=(pipe_read_fd,))
    network_thread.start()
    obs.timer_add(scene_switcher_tick, 1000)
    obs.timer_add(result_processor_tick, 100)
    obs.timer_add(connection_watchdog_tick, 5000)
    script_update(settings)


def script_update(settings):
    global enabled, switch_interval

    enabled = obs.obs_data_get_bool(settings, "enabled")
    new_interval = obs.obs_data_get_double(settings, "switch_interval")
    if new_interval != switch_interval:
        switch_interval = new_interval
        obs.timer_remove(scene_switcher_tick)
        obs.timer_add(scene_switcher_tick, int(switch_interval * 1000))
    current_scene_names = []
    scenes = obs.obs_frontend_get_scenes()
    if scenes:
        for scene in scenes:
            current_scene_names.append(obs.obs_source_get_name(scene))
        obs.source_list_release(scenes)
    for scene_name in current_scene_names:
        if scene_name in scene_managers:
            scene_managers[scene_name].update(settings)
        else:
            scene_managers[scene_name] = SceneManager(scene_name, settings)
    for scene_name in list(scene_managers.keys()):
        if scene_name not in current_scene_names:
            command_queue.put({"command": "disconnect", "scene_name": scene_name})
            del scene_managers[scene_name]


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
    scene_managers.clear()


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

    if best_scene_name:
        # Get the current scene's name in a safe, isolated way
        current_scene_source = obs.obs_frontend_get_current_scene()
        current_scene_name = None
        if current_scene_source:
            current_scene_name = obs.obs_source_get_name(current_scene_source)
            # Immediately release the handle after we're done with it
            obs.obs_source_release(current_scene_source)

        # If the best scene is not the current one, switch to it
        if current_scene_name != best_scene_name:
            best_scene_source = obs.obs_get_source_by_name(best_scene_name)
            if best_scene_source:
                obs.obs_frontend_set_current_scene(best_scene_source)
                log(f"Switched to scene '{best_scene_name}' with score {highest_score}")
                obs.obs_source_release(best_scene_source)
