# Simple-SVT-HEVC-GUI
A simple GUI using PySimpleGUI for the SVT-HEVC FFmpeg plugin.

### External libraries
* [PySimpleGUIQt](https://github.com/PySimpleGUI/PySimpleGUI)
* [pymediainfo](https://github.com/sbraz/pymediainfo)

### External software
* [FFmpeg](https://github.com/FFmpeg/FFmpeg)
* [SVT-HEVC](https://github.com/OpenVisualCloud/SVT-HEVC)

## Binaries and building
The goal is to privde the gui and ffmpeg as binaries in the release section. Currently only Windows build is available.

To create a binary for the UI, use pyinstaller (which can be installed with pip)
For example: `pyinstaller -wF Simple-GUI.py`

FFmpeg built with SVT-HEVC support needs to exist in the same folder as the gui executable or the python script.
Build it with [the instructions from the official SVT-HEVC repo](https://github.com/OpenVisualCloud/SVT-HEVC/tree/master/ffmpeg_plugin) or with the [media-autobuild-suite](https://github.com/m-ab-s/media-autobuild_suite).

## Licenses
All software should be GLP-compatible.
FFmpeg was built with GPL v3 compatibility.

Note that SVT-HEVC is Copyright (c) 2018 Intel Corporation and distribution of its binaries must be accompanied by the license and terms detailed [here](https://github.com/OpenVisualCloud/SVT-HEVC/blob/master/LICENSE.md).
