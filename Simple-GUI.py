"""A GUI for creating and running video encodes with ffmpeg and the SVT-HEVC encoder

The gui program handles setting parameters for ffmpeg, and starts the ffmpeg tasks in a separate thread.
Queues are used to communicate events and tasks between the main and encode thread.

Licenses for modules, libraries and external software used:
SVT-HEVC: GPL v2 - Copyright (c) 2018 Intel Corporation
PySimpleGUI: LGPL v2
pymediainfo: The MIT License
FFmpeg: (L)GPL v3
"""

import json
import queue
import time
import re
import uuid
import threading
import subprocess
from pathlib import Path
from collections import deque

import PySimpleGUIQt as sg
from pymediainfo import MediaInfo

__author__ = "Sondre Kindem"
__email__ = "sondre.kindem@gmail.com"
__license__ = "GPL v3"
__status__ = "dev"

# pyinstaller -wF Simple-GUI.py

#####################
# TODOS
# Interface and functions:
# TODO: make sure that the input is an actual video file
# TODO: make sure timestamps and crop is properly formatted!
# TODO: file and folder batch
# TODO: save presets➠
# TODO: do crop detection
# TODO: mark job as cancelled if encode is stopped - mark with ❌
# TODO: make it more obvious that the queue is paused
# TODO: investigate using ffmpeg bindings for formatting the command. Alternatively create own bindings to simplify
# TODO: decide whether to include a progressbar or not
# TODO: save and load queue

# FFMPEG stuff:
# TODO: implement decomb filter like handbrake - possibly with vapour/avisynth?

# Threading
# TODO: sometimes, especially when crashing, the ffmpeg process will get orphaned

# Misc code stuff
# TODO: size analysis reports wrong track size
# TODO: find a way to detect when encode stopped without finishing or being cancelled
# TODO: program sometimes crashes, prevent this...
# TODO: clean up docstrings and comments
# TODO: make size estimation more accurate
#####################

# Var for handlig stop signal for thread. Kinda shitty but will do for now
stoprequest = threading.Event()
encode_running = threading.Event()
encode_queue_active = threading.Event()

_sentinel = object()
_skip = object()


def calc_time(start_time, end_time):
    """Calculate elapsed time between two times"""
    elapsed_time = end_time - start_time
    return '{}h:{}m:{:.2f}s'.format(int(elapsed_time // 3600), int(elapsed_time % 3600 // 60), elapsed_time % 60), elapsed_time


def format_seconds(seconds):
    """Convert seconds into a string of hours, minutes or seconds, depending on how many seconds there are

    :param seconds: the amount of seconds as a number to be converted"""
    if seconds < 60:
        return "{} seconds".format(int(seconds))
    elif seconds < 3600:
        return "{} minutes".format(round(seconds / 60), 2)
    else:
        return "{:.2f} hours".format(seconds / 3600)


def encode_thread(encode_queue, gui_queue, status_deque, encode_event):
    """A worker thread that communicates with the GUI through queues.

    The thread will wait for the encode_queue to deliver encode params. This way multiple jobs can be queued.

    :param gui_queue: (queue.Queue) Queue to communicate back to GUI that task is completed
    :param status_deque: (collections.deque) Deque used for updating stats during encode
    :param encode_queue: (queue.Queue) Queue which is eventually populated with dicts containing data for starting encodes
    :param encode_event: (queue.Queue) Queue which lets the thread send an event when a job starts and finishes
    :return:
    """
    print("Encode thread initialized")
    while True:
        if not encode_queue_active.is_set():  # This will be false when the program is launched
            encode_queue_active.wait()  # Pause queue execution until the event is set

        params = encode_queue.get()
        if params is _sentinel:  # We need a way to exit even though we are waiting for the queue, so we use the sentinel var
            break
        if params is _skip:  # This lets us skip one turn so we can pause the queue using the threading event
            continue

        # todo: are we sure the values in the dict are always there?
        command = params["command"]
        test_encode = params["test_encode"]
        metadata = params["metadata"]
        job_id = params["uuid"]
        output_file = params["output_file"]
        total_frames = test_encode if test_encode else int(metadata["frame_count"])  # This could still result in None

        done_frames = 0

        print('Starting encode of ' + params["title"] + " - " + job_id)
        encode_event.put({"uuid": job_id, "event": "▶ started"})
        encode_running.set()
        start_time = time.time()
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True, close_fds=True)
        # Print output as it arrives
        print("Processing...")

        # We want to print the initial information from the encoder before we start capturing encode stats
        msg = ""
        for line in process.stdout:
            string = r"{}".format(line)
            if string[:3] == "fra":
                break
            else:
                msg += "{}\n".format(line.strip())
        # Collect the previous lines in one var and then print through queue,
        # if we don't the output element will try to print two lines at the same time and crash... I think...
        # gui_queue.put(msg)
        gui_queue.put("START ENCODE VIDEO")

        # Once the initial information has been fetched we can start updating the status box
        # It is not super important for this information to be exactly realtime. It only fetches the latest output line each iteration
        # and the status deque only holds the latest line, so there is no actual queue waiting.
        while True:
            if stoprequest.isSet():
                gui_queue.put("Reiceived kill signal, stopping...")
                process.kill()
                break

            line = process.stdout.readline()
            if line == '' and process.poll() is not None:
                break
            if not line[:3] == "fra":
                break
            if line:
                split = re.split("frame=|fps=|q=|size=|time=", line)  # index 1 = frame, 2 = fps, 3 = q, 4 = size  TODO: investigate instances when the 'q=' split ends with 'L' forming Lsize?

                percent_done = float(0)  # (and est_size) Initialized as a string, in case we are unable to calculate percent done
                est_size = float(0)
                done_frames = int(split[1])
                time_to_complete = ""

                # TODO: only change total_frames in if-elif, the rest can be outside
                if total_frames:
                    # print("{} of {}".format(done_frames, total_frames))
                    percent_done = 100 - (((total_frames - done_frames) / total_frames) * 100)

                    # print("frames:", total_frames, " done f:", done_frames, "hm:", (time.time() - start_time))

                    time_to_complete = format_seconds((total_frames - done_frames) * (time.time() - start_time) / (1 if done_frames == 0 else done_frames))
                    # progressbar.UpdateBar(done_frames, max=total_frames)

                if type(percent_done) is not str and percent_done > 0:
                    cur_size = int(split[4].replace("kB", ""))
                    est_size = ((cur_size * 100) / percent_done) / 1024  # size in MiB

                formatted_time, seconds = calc_time(start_time, time.time())
                status_deque.append("frame: {}/{} | fps: {} | done: {:.1f}% | est. size: {:.2f} | elapsed: {} | time: {}".format(split[1], total_frames, split[2], percent_done, est_size, formatted_time, time_to_complete))

        size_analysis = ""
        media_info = MediaInfo.parse(str(output_file.absolute()))
        for track in media_info.tracks:
            if track.track_type == 'Video':
                if track.stream_size and metadata["size"]:
                    final_size = int(track.stream_size) / 1048576  # in MiB
                    diff = metadata["size"] - final_size
                    size_analysis = "Final size: {:.2f} MB, saving {:.2f} MB. A size reduction of {:.2f}%".format(final_size, diff, (diff / metadata["size"]) * 100)
                # Below not working because mediainfo lists some "fromstats" tags that are the same as the original file for some reason
                # elif track.frame_count and total_frames and int(track.frame_count) != total_frames and not stoprequest.is_set():

                # # CODE FOR automatically adding a job if the encoded frames did not match the total num of frames. Broken when changing end time
                # if 0 < done_frames < total_frames - 300 and total_frames and not stoprequest.is_set():
                #     gui_queue.put("Did not encode the expected amount of frames... restarting encode...\nThis feature is experimental and might mess everything up.\n")
                #     old = [params]
                #     while True:
                #         try:
                #             old.append(encode_queue.get_nowait())
                #         except queue.Empty:
                #             break
                #     [encode_queue.put(i) for i in old]
                break

        end_string = '** Finished encode of {}.\nDuration: {}\n{} frames **'.format(params["title"], calc_time(start_time, time.time())[0], done_frames)

        encode_running.clear()
        stoprequest.clear()

        encode_event.put({"uuid": job_id, "event": "✓ finished"})
        gui_queue.put(end_string + "\n{}\n".format(size_analysis))  # put a message into queue for GUI
        status_deque.append(end_string)  # Put a message in status box


def check_paths(ffmpeg_path, gui_queue):
    """Notify wether all required external tools exist.

    :param ffmpeg_path: (pathlib.Path) the filepath to the ffmpeg executable
    :param gui_queue: (Queue) a queue for handling printing
    """
    if not ffmpeg_path.exists() or not ffmpeg_path.is_file():
        gui_queue.put("MISSING ffmpeg! Is ffmpeg at " + ffmpeg_path.absolute() + "?")
    else:
        gui_queue.put("Found ffmpeg")
        print(ffmpeg_path.absolute())


def clear_queue(q):
    """Thread-safe removal of all items in a queue"""
    with q.mutex:
        q.queue.clear()


def run_themes_window():
    """Let the user preview and pick a theme from a list of themes.

    :return: the name of the selected theme, or None if no theme selected
    """
    window_bg = 'lightblue'

    def sample_layout(key):
        return [[sg.Text('Text element', tooltip="I am a tooltip!")],
                [sg.Button('Select', key=key, tooltip="This is a tooltip")]]

    layout = [[sg.Text('Select a theme. Program must be restarted for changes to take effect', auto_size_text=True)]]

    names = sg.list_of_look_and_feel_values()
    names.sort()
    row = []
    for count, theme in enumerate(names):
        sg.change_look_and_feel(theme)
        if not count % 9:
            layout += [row]
            row = []
        row += [sg.Frame(theme, sample_layout(key=theme))]
    if row:
        layout += [row]

    theme_window = sg.Window('All themes', layout, background_color=window_bg)
    event, vals = theme_window.read()

    theme_window.close()
    del theme_window
    return event


def write_settings(settings_path, settings):
    with settings_path.open("w") as file:
        json.dump(settings, file)


def the_gui():
    """Starts and executes the GUI

    Returns when the user exits / closes the window
    """
    ffmpeg_path = Path('ffmpeg_hevc.exe')
    settings_path = Path("settings.json")
    presets = {0: "placebo", 1: "placebo", 2: "placebo", 3: "placebo", 4: "veryslow", 5: "slower", 6: "slow", 7: "medium", 8: "fast", 9: "faster", 10: "veryfast", 11: "superfast", 12: "ultrafast"}

    # queue used to communicate between the gui and the threads
    gui_queue = queue.Queue()

    encode_queue = queue.Queue()
    encode_list = []

    encode_event = queue.Queue()  # queue for handling when encodes finish and start

    # Define default settings to make it possible to generate settings.json
    settings = {"settings": {"theme": "Default1"}}

    status_deque = deque(maxlen=1)  # deque for handling the status bar element

    check_paths(ffmpeg_path, gui_queue)

    # Load settings from json if it exist
    if settings_path.exists():
        with settings_path.open() as file:
            settings = json.load(file)
            sg.change_look_and_feel(settings["settings"]["theme"])
    else:
        print("Could not find settings.json")
        write_settings(settings_path, settings)

    tooltips = {
        # ENCODE
        "tune": "0 = visual quality, 1 = psnr/ssim, 2 = vmaf",
        "preset": "Trades speed for quality and compression.\n'Veryfast' and up only works for 1080p or higher, while 'ultrafast' and 'superfast' is 4k and higher only.\nYou might have a file that is just a few pixels away fitting 1080p, and you will be limited to the 'faster' preset or slower\nBest to stay around medium unless you have a godly pc/trash pc",
        "drc": "Enables variable bitrate. Lets the encoder vary the targeted quality. \nIf you are unsure, leave it off",
        "qmin": "Minimum quality level in DRC mode. Must be lower than qmax",
        "qmax": "Maximum quality level in DRC mode. Must be higher than qmin",
        "qp": "The quality the encoder will target. Lower values result in higher quality, but much larger filesize. \nUsually stay around 20 for h.264 content, but can often go lower for mpeg2 content. Recommend 22 or 23 for 1080p with high bitrate. \nExperiment with quality by enabling test encode!",
        # FILTERS
        "sharpen": "How much to sharpen the image with unsharp, with a moderate impact to encode speed. \nIt is usually a good idea to sharpen the image a bit when transcoding. I recommend about 0.2 to 0.3",
        # AUDIO
        "skip_audio": "Enable to skip all audio tracks. Disable to passthrough audio",
        # BUTTONS
        "pause_queue": "Once the queue is paused the current job will finish, but the next job will not be started.",
        "start_encode": "Add job to queue, start it if no encode is currently running.",
        # MISC
        "test_encode": "Only encode part of the video. Lets you compare quality of encode to source, and estimate filesize. \nSpecify how many frames, usually 1000 is enough"
    }

    params = {
        "input": "",
        "output": "",
        "skip_audio": "",
        "qp": 20,
        "subtitles": False,
        "enable_filters": "-vf",
        "drc": 0,
        "qmin": 19,
        "qmax": 21,
        "sharpen_mode": "",
        "crop": "",
        "tune": 0,
        "preset": 7,
        "test_encode": "",
        "n_frames": "1000",
        "start_time": "00:00:00.000",
        "end_time": "",
    }

    old_params = params.copy()

    video_metadata = {
        "contains_video": False,
        "frame_count": None,
        "size": None,
        "fps": None,
        "duration": None,
        "width": None,
        "height": None,
    }

    menu_def = [['&Settings', ['&Themes', '!&Preferences', '---', 'E&xit']], ['&Presets', ['!&Save preset', '---', '!Preset1']]]

    drc_col = [
        [
            sg.Checkbox("Dynamic rate control", key="-DRC-", enable_events=True, tooltip=tooltips["drc"])
        ],
        [
            sg.Text("minQP", size=(5, 1), tooltip=tooltips["qmin"]),
            sg.Spin([i for i in range(1, 51)], initial_value=params["qmin"], key="-QMIN-", size=(5, 1), enable_events=True, disabled=True, tooltip=tooltips["qmin"]),
            sg.Text("maxQP", size=(5, 1), tooltip=tooltips["qmax"]),
            sg.Spin([i for i in range(1, 51)], initial_value=params["qmax"], key="-QMAX-", size=(5, 1), enable_events=True, disabled=True, tooltip=tooltips["qmax"]),
        ],
    ]

    encoding_col = [
        [
            sg.Column([
                [sg.Text("Quality", tooltip=tooltips["qp"])],
                [sg.Text("QP", size=(2, 1)), sg.Spin([i for i in range(1, 51)], initial_value=params["qp"], key="-QP-", size=(5, 1), enable_events=True, tooltip=tooltips["qp"])],
            ]),
            sg.VerticalSeparator(),
            sg.Column(drc_col),
            sg.VerticalSeparator(),
            sg.Column([[sg.Text("Preset (medium)", size=(10, 1), key="-PRESET_TEXT-", tooltip=tooltips["preset"])], [sg.Slider(range=(0, 12), default_value=7, orientation="horizontal", key="-PRESET-", enable_events=True, tooltip=tooltips["preset"])]]),
            # sg.Column([[sg.Text("Tune", size=(5, 1), tooltip=tooltips["tune"])], [sg.DropDown([0, 1, 2], default_value=0, key="-TUNE-", enable_events=True, tooltip=tooltips["tune"])]])  # Tune is no longer an option
         ],
    ]

    audio_col = [
        [
            sg.Checkbox("Skip audio", key="-AUDIO-", enable_events=True, default=False, tooltip=tooltips["skip_audio"])
        ]
    ]

    def range_with_floats(start, stop, step):
        """Func for generating a range of decimals"""
        while stop > start:
            yield round(start, 2)  # adding round() to our function and rounding to 2 digits
            start += step

    filter_col = [
        [
            sg.Checkbox("Sharpen", key="-SHARP_CONTROL-", size=(8, 1), enable_events=True, tooltip=tooltips["sharpen"]), sg.Spin([i for i in range_with_floats(-1.50, 1.50, 0.05)], key="-SHARPEN-", initial_value=0.25, size=(6, 1), enable_events=True, disabled=True, tooltip=tooltips["sharpen"])
        ]
    ]

    video_col = [
        [
            sg.T("Resolution"), sg.Input(default_text="WDxHG", disabled=True, key="-RESOLUTION-"), sg.T("Crop"), sg.Input(key="-CROP-", enable_events=True), sg.Button("Autocrop")
        ]
    ]

    layout = [
        [sg.Menu(menu_def)],
        [sg.Text("Browse or drag n drop video file")],
        [sg.Text("Input"), sg.Input(key="-INPUT-", enable_events=True), sg.FileBrowse(enable_events=True)], #
        [sg.Text("Output"), sg.Input(key="-OUTPUT-", enable_events=True), sg.SaveAs(enable_events=True)],
        [sg.Frame("Encode options", encoding_col)],
        [sg.Frame("Audio options", audio_col), sg.Frame("Filters", filter_col)],
        [sg.Frame("Video", video_col)],
        [sg.Frame("Misc", [[sg.Checkbox("Test encode (n frames)", size=(16, 1), key="-TEST_ENCODE-", enable_events=True, tooltip=tooltips["test_encode"]), sg.Input(default_text=params["n_frames"], size=(5, 1), enable_events=True, key="-TEST_FRAMES-", disabled=True, tooltip=tooltips["test_encode"])], [sg.T("Start time", size=(7, 1)), sg.Input(default_text=params["start_time"], enable_events=True, key="-START_TIME-", size=(9, 1), tooltip="Start timestamp"), sg.T("End time", size=(6, 1)), sg.Input(default_text="00:00:00.000", enable_events=True, key="-END_TIME-", size=(9, 1), tooltip="End timestamp")]])],
        # [sg.Frame("Command", [[sg.Column([[sg.Multiline(key="-COMMAND-", size=(60, 3))]])]])],
        [sg.Frame("Queue", [[sg.Column([[sg.Listbox(values=[], key="-QUEUE_DISPLAY-")], [sg.Button("Remove task", size=(15, 1)), sg.Button("UP", size=(7, 1)), sg.Button("DOWN", size=(7, 1))]])]])],
        [sg.Button("Start encode / add to queue", key="Start encode", size=(20, 1), tooltip=tooltips["start_encode"]), sg.Button("Stop encode", size=(20, 1)), sg.Button("Pause queue", key="Pause queue", size=(20, 1), tooltip=tooltips["pause_queue"])],
        [sg.T("", key="-STATUS_BOX-")],
        [sg.Output()],  # Disable this line to get output to the console
        # [sg.ProgressBar(100, key="-PROGRESSBAR-")],
        [sg.Button('Exit')],
    ]

    window = sg.Window('SVT_GUI', layout)

    encoder = threading.Thread(target=encode_thread, args=(encode_queue, gui_queue, status_deque, encode_event), daemon=True)
    try:
        encoder.start()
    except Exception as e:
        print('Error starting work thread. Bad input?\n ' + str(e))

    encode_queue_active.set()  # Start active

    # progressbar = window["-PROGRESSBAR-"]

    def update_command():
        window["-COMMAND-"].update(' '.join(format_command()))

    def format_command():
        input_text = ""
        output_text = ""

        if params["input"] != "":
            input_path = Path(params["input"])
            input_text = str(input_path)

        if params["output"] != "":
            output_path = Path(params["output"])
            output_text = str(output_path)

        enable_filters = params["enable_filters"] if params["sharpen_mode"] != "" or params["crop"] else ""  # todo: Add check for each filter here

        filters = ",".join(filter(None, [params["sharpen_mode"], params["crop"]]))
        print("filters: " + (filters if filters else "None"))

        n_frames = params["n_frames"] if params["test_encode"] != "" else ""  # Disable vframes number if we dont want to do test encode

        # Filter list before return to remove empty strings
        return list(filter(None, ["-i", input_text, "-y", "-ss", params["start_time"], ("-to" if params["end_time"] else ""), params["end_time"], "-sn", params["skip_audio"], "-map", "0", enable_filters, filters, "-c:v", "libsvt_hevc", params["test_encode"], n_frames, "-rc", str(params["drc"]), "-qmin", str(params["qmin"]), "-qmax", str(params["qmax"]), "-qp", str(params["qp"]), "-preset", str(params["preset"]), output_text]))

    def toggle_queue():
        if encode_queue_active.is_set():
            encode_queue_active.clear()
            window.Element("Pause queue").update("Unpause Queue")
        else:
            encode_queue_active.set()
            window.Element("Pause queue").update("Pause Queue")

    def pause_queue():
        if encode_queue_active.is_set():
            encode_queue_active.clear()
            window.Element("Pause queue").update("Unpause Queue")

    def autocrop():
        try:
            # TODO: If the video is shorter than around 16 seconds we might not get any crop values because of the low framerate and start time
            start_time = int((video_metadata["duration"] / 4) / 1000)  # Start detecting crop at 1/4 of the video duration
            command = [ffmpeg_path.absolute().as_posix(), "-ss", str(start_time), "-i", str(Path(params["input"])), "-t", "01:20", "-vsync",
                       "vfr", "-vf", "fps=0.2,cropdetect", "-f", "null", "-"]
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                       universal_newlines=True, close_fds=True)
            # out, err = process.communicate()
            crop_values = []
            for line in process.stdout:
                # print(line)
                if "crop=" in line:
                    crop_values.append(line.split("crop=")[1])

            if len(crop_values) > 0:
                most_common = max(set(crop_values), key=crop_values.count)
                print("CROP: " + most_common)
                if most_common:
                    return most_common
                else:
                    print("Could not generate a crop :(")
                    return ""

        except Exception as ex:
            print(ex.args)
        print("Could not generate a crop :(")
        return ""

    def update_queue_display():
        window.Element("-QUEUE_DISPLAY-").update(values=[i["status"] + " | " + i["title"] + " - " + i["uuid"] for i in encode_list])

    def build_encode_queue():
        clear_queue(encode_queue)
        for i in encode_list:
            if i["status"] == "⏱ waiting":
                encode_queue.put(i)

    #                                                        #
    # --------------------- EVENT LOOP --------------------- #
    while True:
        event, values = window.read(timeout=100)
        if event in (None, 'Exit'):
            break

        elif event == "-INPUT-":
            window.Element("-INPUT-").update(background_color="white")  # Reset background color
            file_string = values["-INPUT-"].replace("file:///", "")
            input_file = Path(file_string)
            if input_file.exists() and input_file.is_file():
                params["input"] = input_file.absolute()  # Update params

                # Fill in output based on folder and filename of input
                new_file = input_file
                while new_file.exists():
                    new_file = Path(new_file.with_name(new_file.stem + "_new.mkv"))
                params["output"] = str(new_file.absolute())
                window.Element("-OUTPUT-").update(str(new_file.absolute()))

                print("** Analyzing input using mediainfo... **")
                media_info = MediaInfo.parse(str(input_file.absolute()))

                for track in media_info.tracks:
                    if track:
                        if track.track_type == "General":
                            video_metadata["name"] = track.file_name_extension
                        elif track.track_type == 'Video':
                            video_metadata["contains_video"] = True
                            video_metadata["frame_count"] = track.frame_count
                            video_metadata["size"] = int(track.stream_size) / 1048576 if track.stream_size else None  # in MiB
                            video_metadata["fps"] = track.frame_rate
                            video_metadata["width"] = track.width
                            video_metadata["height"] = track.height

                            # Reset the start and end time params
                            params["end_time"] = ""
                            params["start_time"] = "00:00:00.000"

                            if track.height and track.width:
                                window.Element("-RESOLUTION-").update("%ix%i" % (track.width, track.height))

                            if track.duration:
                                video_metadata["duration"] = float(track.duration)

                                # hours, rem = divmod(float(track.duration), 3600)
                                # minutes, seconds = divmod(rem, 60)
                                milliseconds = float(track.duration)
                                seconds = (milliseconds / 1000) % 60
                                minutes = int((milliseconds / (1000*60)) % 60)
                                hours = int((milliseconds / (1000*60*60)) % 24)
                                formatted_duration = "{:0>2}:{:0>2}:{:06.3f}".format(hours, minutes, seconds)
                                print("Duration:", formatted_duration)
                                window.Element("-END_TIME-").update(disabled=False)
                                window.Element("-END_TIME-").update(formatted_duration)
                            else:
                                window.Element("-END_TIME-").update(disabled=True)

                if video_metadata["frame_count"] is None and video_metadata["contains_video"]:
                    print("Could not extract frame count, will not be able to report progress %")
                if not video_metadata["contains_video"]:
                    print("This file is either not a video file or does not contain a video stream.")
                    window.Element("-INPUT-").update(background_color="red")

                print('** Analyze done **')

            else:
                print("Can't find file: " + str(input_file.absolute()))

        elif event == "-OUTPUT-":
            if values["-OUTPUT-"] == "":  # If the user clicks the saveAs button and cancels, the output string will be empty. Better to keep the old value in that case
                window.Element("-OUTPUT-").update(params["output"])
            else:
                file_string = values["-OUTPUT-"].replace("file:///", "")
                params["output"] = file_string

        ##################
        # ENCODE SETTINGS
        elif event == "-QP-":
            params["qp"] = values["-QP-"]

        elif event == "-DRC-":
            val = values["-DRC-"]
            if val:
                params["drc"] = 1
                window.Element("-QMIN-").update(disabled=False)
                window.Element("-QMAX-").update(disabled=False)
                window.Element("-QP-").update(disabled=True)
            else:
                params["drc"] = 0
                window.Element("-QMIN-").update(disabled=True)
                window.Element("-QMAX-").update(disabled=True)
                window.Element("-QP-").update(disabled=False)

        elif event == "-PRESET-":  # TODO: handle limiting preset by resolution as per https://github.com/OpenVisualCloud/SVT-HEVC/blob/master/Docs/svt-hevc_encoder_user_guide.md#encoding-presets-table
            window.Element("-PRESET_TEXT-").update("Preset ({})".format(presets[values["-PRESET-"]]))
            params["preset"] = values["-PRESET-"]

        elif event == "-TEST_ENCODE-":
            val = values["-TEST_ENCODE-"]
            if val:
                window.Element("-TEST_FRAMES-").update(disabled=False)
                params["test_encode"] = "-vframes"
            else:
                window.Element("-TEST_FRAMES-").update(disabled=True)
                params["test_encode"] = ""

        elif event == "-TEST_FRAMES-":
            val = ''.join(i for i in values["-TEST_FRAMES-"] if i.isdigit())  # Remove any non numbers from the input
            window.Element("-TEST_FRAMES-").update(val)
            params["n_frames"] = val

        elif event == "-START_TIME-":
            params["start_time"] = values["-START_TIME-"]

        elif event == "-END_TIME-":
            params["end_time"] = values["-END_TIME-"]

        ##################
        # AUDIO SETTINGS
        elif event == "-AUDIO-":
            if values["-AUDIO-"]:
                params["skip_audio"] = "-an"
            else:
                params["skip_audio"] = ""

        ##################
        # FILTER SETTINGS
        elif event == "-SHARPEN-":
            params["sharpen_mode"] = "unsharp=5:5:{}:5:5:{}".format(values["-SHARPEN-"], values["-SHARPEN-"])

        elif event == "-CROP-":
            if values["-CROP-"]:
                params["crop"] = "crop=" + values["-CROP-"]
            else:
                params["crop"] = ""

        elif event == "-SHARP_CONTROL-":
            if values["-SHARP_CONTROL-"]:
                window.Element("-SHARPEN-").update(disabled=False)
                params["sharpen_mode"] = "unsharp=5:5:{}:5:5:{}".format(values["-SHARPEN-"], values["-SHARPEN-"])
            else:
                window.Element("-SHARPEN-").update(disabled=True)
                params["sharpen_mode"] = ""

        ##################
        # QUEUE BUTTONS
        elif event == "Remove task":
            if values["-QUEUE_DISPLAY-"]:
                for queue_item in values["-QUEUE_DISPLAY-"]:  # TODO: make alternative to nesting loops
                    job_id = queue_item.split()[-1]
                    for i, job in enumerate(encode_list):
                        if job["uuid"] == job_id and job["status"] != "▶ started":
                            encode_list.pop(i)

            build_encode_queue()
            update_queue_display()

        elif event == "UP":
            if values["-QUEUE_DISPLAY-"] and len(values["-QUEUE_DISPLAY-"]) == 1 and len(encode_list) > 1:
                job_id = values["-QUEUE_DISPLAY-"][0].split()[-1]
                for i, job in enumerate(encode_list):
                    if job["uuid"] == job_id and i != 0:
                        encode_list.insert(i - 1, encode_list.pop(i))

                build_encode_queue()
                update_queue_display()

        elif event == "DOWN":
            if values["-QUEUE_DISPLAY-"] and len(values["-QUEUE_DISPLAY-"]) == 1 and len(encode_list) > 1:
                job_id = values["-QUEUE_DISPLAY-"][0].split()[-1]
                for i, job in enumerate(encode_list):
                    if job["uuid"] == job_id and i != len(encode_list)-1:
                        encode_list.insert(i + 1, encode_list.pop(i))

                build_encode_queue()
                update_queue_display()

        ##################
        # OTHER INTERACTS
        elif event == "Start encode":
            if not params["input"] or params["input"] == "":
                print("Missing input")
            elif not params["output"] or params["output"] == "":
                print("Missing output")
            elif not video_metadata["contains_video"]:
                print("Cannot start encode because input file does not have a video track")
            else:
                finished_command = [ffmpeg_path.absolute().as_posix()] + format_command()

                try:
                    job_id = uuid.uuid4().hex
                    encode_list.append({"title": video_metadata["name"], "uuid": job_id, "status": "⏱ waiting", "output_file": Path(params["output"]), "command": finished_command, "metadata": video_metadata, "test_encode": int(params["n_frames"]) if params["test_encode"] != "" else False})
                    build_encode_queue()
                    update_queue_display()
                except Exception as e:  # TODO: make this better. Is it even needed?
                    print('Error adding job. Bad input?:\n "%s"' % finished_command)

        elif event == "Stop encode":
            if encoder is not None:
                stoprequest.set()
                pause_queue()

        elif event == "Pause queue":
            toggle_queue()
            encode_queue.put(_skip)

        elif event == "Autocrop":
            crop = autocrop()
            params["crop"] = "crop=" + crop
            window.Element("-CROP-").update(crop)

        elif event == "Themes":
            theme = run_themes_window()
            # Save theme
            if theme and settings_path.exists():
                settings["settings"]["theme"] = theme
                write_settings(settings_path, settings)
                print("Changed theme to {}. Restart the program to apply.".format(theme))

        try:
            window.Element("-STATUS_BOX-").update(status_deque.popleft())
        except IndexError:
            pass

        # --------------- Check for incoming messages from threads  ---------------
        try:
            message = gui_queue.get_nowait()
        except queue.Empty:             # get_nowait() will get exception when Queue is empty
            message = None              # break from the loop if no more messages are queued up

        try:
            # Check for job events
            event = encode_event.get_nowait()
            print(event)
            for i, job in enumerate(encode_list):
                if job["uuid"] == event["uuid"]:
                    item = encode_list[i]
                    item["status"] = event["event"]
                    encode_list[i] = item
                    update_queue_display()
        except queue.Empty:
            pass

        # if message received from queue, display the message in the Window
        if message:
            print("#> " + message)

        # Update display of the encode command
        # if params.items() != old_params.items():
        #     update_command()
        #     old_params = params.copy()

    # We have reached the end of the program, so lets clean up.
    window.disable()
    window.refresh()  # have to refresh window manually outside of event loop
    if encoder is not None:
        stoprequest.set()
        # Clear queue then add sentinel to make thread stop waiting
        clear_queue(encode_queue)
        encode_queue.put(_sentinel)
        encode_queue_active.set()  # We have to make sure the encode queue is active for it to finish, if not it will keep waiting

        print("\n** Taking a sec to shut everything down... **\n")
        window.refresh()

        encoder.join()
        if encoder.is_alive():
            print("Thread still alive! wtf")
            window.refresh()

    window.close()


if __name__ == '__main__':
    the_gui()
