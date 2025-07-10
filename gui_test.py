import obspython as obs

MAX_SCENES = 20
num_scenes = 10


def add_scene_pressed(props, prop):
    """Called when the 'Add Scene' button is pressed."""
    global num_scenes
    if num_scenes < MAX_SCENES:
        p_group = obs.obs_properties_get(props, f"scene_{num_scenes}")
        obs.obs_property_set_visible(p_group, True)
        num_scenes += 1
        return True
    return False


def remove_scene_pressed(props, prop):
    """Called when the 'Remove Scene' button is pressed."""
    global num_scenes
    if num_scenes > 0:
        num_scenes -= 1
        p_group = obs.obs_properties_get(props, f"scene_{num_scenes}")
        obs.obs_property_set_visible(p_group, False)
        return True
    return False


def script_properties():
    """Called to define script properties."""
    props = obs.obs_properties_create()

    obs.obs_properties_add_button(props, "add_button", "Add Scene", add_scene_pressed)
    obs.obs_properties_add_button(
        props, "remove_button", "Remove Scene", remove_scene_pressed
    )

    obs.obs_properties_add_float(
        props, "scene_switch_interval", "Scene Switch Interval (seconds)", 0, 60, 0.01
    )

    for i in range(MAX_SCENES):
        group = obs.obs_properties_create()
        p_group = obs.obs_properties_add_group(
            props, f"scene_{i}", f"Scene {i+1}", obs.OBS_GROUP_NORMAL, group
        )
        obs.obs_properties_add_text(
            group, f"scene_name_{i}", "Scene Name", obs.OBS_TEXT_DEFAULT
        )
        obs.obs_properties_add_text(
            group, f"address_{i}", "Address (optional)", obs.OBS_TEXT_DEFAULT
        )
        obs.obs_property_set_visible(p_group, i < num_scenes)

    return props


def script_load(settings):
    """Called when the script is loaded."""
    global num_scenes
    num_scenes = obs.obs_data_get_int(settings, "num_scenes")


def script_save(settings):
    """Called when the script is saved."""
    global num_scenes
    obs.obs_data_set_int(settings, "num_scenes", num_scenes)


def script_defaults(settings):
    """Called to set default values."""
    obs.obs_data_set_int(settings, "num_scenes", 10)
