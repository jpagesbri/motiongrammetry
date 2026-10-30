"""Named option sets for the capture problems that come up most often.

Each preset only sets what differs from the defaults; explicit options still win.
"""

PRESETS: dict[str, dict] = {
    "low-texture": {
        "description": "Dark, plain or shiny subject on a featureless surface (black mouse on a white desk). "
                       "More, weaker features and a wider matching window keep the frames connected.",
        "options": {"sift_peak": 0.001, "max_features": 16000, "max_image_size": 2400, "overlap": 20,
                    "loop_stride": 3, "num": 180},
    },
    "turntable": {
        "description": "Camera fixed, object rotating on a turntable: the background must not be used for "
                       "poses, and the support (turntable) is removed with the masks.",
        "options": {"mask_sfm": True, "mapper": "incremental", "matcher": "exhaustive", "up": "camera"},
    },
    "blurry": {
        "description": "Fast or low-light capture with motion blur: analyse every frame and reject blur harder.",
        "options": {"sample_fps": 30.0, "blur_reject": 0.55, "num": 120, "overlap": 16},
    },
    "person": {
        "description": "A person standing or sitting still: portrait segmentation, more frames, and keep "
                       "thin parts (hair, fingers) by carving gently.",
        "options": {"mask_model": "person", "num": 200, "mask_dilate": 8, "bg_tolerance": 0.25},
    },
    "glossy-desk": {
        "description": "Reflective support surface: reflections reconstruct as geometry under the desk and "
                       "masks leak into them; cut a little more above the plane and carve more gently.",
        "options": {"ground_margin": 0.05, "bg_tolerance": 0.25},
    },
    "drone": {
        "description": "Drone orbit of something big outdoors (a castle, a bridge, a building): the subject "
                       "masks pick out only parts of a big structure in each view, so carving is very gentle "
                       "(the water or ground plane and the masks' consensus still remove the surroundings); "
                       "more keyframes and resolution from the 4K video, a lighter output mesh.",
        "options": {"num": 180, "max_side": 2400, "mask_model": "object", "bg_tolerance": 0.75,
                    "target_faces": 1500000},
    },
    "low-disk": {
        "description": "Little free disk or RAM: quarter-resolution depth maps (16x less space than full), "
                       "fewer keyframes, lighter output mesh.",
        "options": {"resolution_level": 2, "num": 100, "target_faces": 150000, "texture_resolution": 1},
    },
    "quick-preview": {
        "description": "Fast sanity check of a capture before a full run (a few minutes on CPU).",
        "options": {"num": 40, "resolution_level": 2, "max_image_size": 1000, "texture_resolution": 1},
    },
}
