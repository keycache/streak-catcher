import os
from dataclasses import dataclass, field, fields
from pathlib import Path


@dataclass(frozen=True)
class Config:
    # hotkey (pynput vk codes)
    combo_vks: frozenset[int] = frozenset({59, 56, 58})  # L-Ctrl, L-Shift, L-Option

    # streak
    streak_fade_s: float = 0.50
    streak_sample_hz: int = 120
    streak_core_rgba: tuple = (1.0, 1.0, 1.0, 0.95)  # white core
    streak_glow_rgba: tuple = (0.35, 0.9, 1.0, 1.0)  # cyan glow
    streak_widths: tuple = (18.0, 8.0, 3.0)  # outer glow, inner glow, core

    # screen capture
    screen_capture_enabled: bool = True  # False: no screenshots are taken during sessions
    # Event-driven capture (Phase 10): frames are grabbed into an in-memory buffer; only chosen ones are saved.
    frame_buffer_interval_s: float = 0.5  # grab cadence
    frame_buffer_s: float = 4.0  # keep at least this much history (plus the whole stroke in progress)
    frame_buffer_max: int = 60  # hard cap on buffered frames (~500 KB each)
    safety_net_interval_s: float = 2.0  # also save a frame this often (dropped if it duplicates a saved one)
    app_switch_settle_s: float = 0.3  # wait this long after the frontmost app changes before saving a frame
    dedup_thumb_width: int = 256  # grayscale thumbnail width used for duplicate checks
    dedup_pixel_delta: int = 24  # gray-level difference that counts a thumbnail pixel as changed
    dedup_changed_pct: float = 3.0  # frames with less changed area than this are duplicates
    frames_include_streak: bool = False  # False = clean frames (streak excluded); annotations are drawn later
    # Frame size relative to native Retina pixels: 0.5 = logical screen size (~490 KB/frame
    # instead of ~1.3 MB at 1.0, text still readable). 1.0 keeps full resolution.
    capture_scale: float = 0.5
    jpeg_quality: float = 0.85

    # audio (must match the asr module, provided later; importing it would load torch/NeMo)
    sample_rate: int = 16000
    channels: int = 1
    chunk_size: int = 1600  # 100 ms
    # Input device: None = system default, otherwise a case-insensitive substring of the
    # device name (e.g. "MacBook Pro Microphone"). Device names are logged at session start.
    audio_input_device: str | None = None

    # output
    captures_dir: Path = Path(__file__).parent / "captures"
    # A session starting within this many seconds after the previous one stopped
    # continues in the same capture folder. 0 disables reuse.
    session_reuse_window_s: float = 10.0

    # speech recognition (cactus Whistle)
    cactus_telemetry: bool = False  # cactus sends anonymous usage counts unless disabled
    asr_language: str | None = "en"  # None = auto-detect (en, de, fr, es, it, nl, pl)
    asr_keywords: tuple = ()  # words/phrases to bias recognition toward
    asr_chunk_s: float = 1.0  # audio fed to Whistle.stream() per pass
    model_wait_timeout_s: float = 60.0  # how long a session's audio waits for the model to load

    # gestures (cursor path; points = global Cocoa coordinates). Tune from recorded path.json files.
    gesture_speed_window_s: float = 0.05  # speed measured over this window (mouse events can repeat samples)
    gesture_pause_speed_pt_s: float = 60.0  # slower than this counts as paused
    gesture_pause_s: float = 0.15  # a pause this long ends a stroke
    gesture_resample_pt: float = 4.0  # path resampling spacing before measuring turns
    gesture_min_size_pt: float = 30.0  # ignore smaller gestures
    gesture_merge_iou: float = 0.5  # circles in separate strokes (pause between) merge at this IoU
    # Write debug_gesture_N.jpg (gesture path + box on the nearest frame) after each session. Defaults to STREAK_DEBUG.
    gesture_debug_images: bool = field(default_factory=lambda: os.environ.get("STREAK_DEBUG") == "1")
    gesture_merge_overlap: float = 0.6  # loops in one continuous stroke merge when overlap/smaller-area >= this
    circle_min_turn_deg: float = 300.0  # cumulative turning needed for a loop
    circle_close_ratio: float = 0.35  # end-to-start gap / bbox diagonal
    circle_max_aspect: float = 3.0  # bbox long side / short side
    underline_max_dy_ratio: float = 0.3  # height / width of the underline
    underline_min_dx_pt: float = 60.0  # minimum underline width
    underline_min_leg_pt: float = 30.0  # each back-and-forth leg at least this wide (rejects overshoot fixes)
    underline_min_reversals: int = 1  # direction changes needed (1 = there and back)
    underline_band_pt: float = 12.0  # robust line fit: points within this vertical distance are on the underline
    underline_max_slope: float = 0.25  # steepest underline considered (dy/dx, ~14°)
    underline_max_gap_pts: int = 6  # resampled outlier points tolerated inside the underline before it is cut

    # request assembly (Phase 11): one request per release
    context_changed_pct: float = 15.0  # frames differing less than this (changed area) share a context/image
    assembly_timeout_s: float = 5.0  # wait this long for the final transcript after release
    assembly_status_after_s: float = 0.3  # show "Transcribing…" only if the wait is longer than this
    speech_min_rms: float = 0.008  # words over quieter audio are dropped (Whistle hallucinates "you" on silence)
    speech_rms_margin_s: float = 0.05  # audio window around each word for the loudness check
    max_images: int | None = None  # None = no cap on context images per request
    truncate_keep: str = "end"  # when capped: keep the most recent ("end") or earliest ("start") contexts
    annotation_rgba: tuple = (1.0, 0.1, 0.45, 1.0)  # numbered gesture boxes on context images
    annotation_pad_pt: float = 8.0  # box padding around the gesture bbox
    annotation_halo_rgba: tuple = (1.0, 1.0, 1.0, 1.0)  # contrasting outline under boxes/badges (visible on any background)
    annotation_halo_pt: float = 2.0
    annotation_line_pt: float = 3.0
    annotation_label_pt: float = 16.0
    no_instructions_caption_s: float = 1.5  # "No instructions heard" caption duration

    # LLM (Phase 12)
    # "copilot" | "echo" (offline, describes the request) | "none" (don't send). STREAK_LLM_PROVIDER overrides.
    llm_provider: str = field(default_factory=lambda: os.environ.get("STREAK_LLM_PROVIDER", "copilot"))
    llm_model: str = "claude-sonnet-5.5" #"gemini-3.8-flash"  # must support vision; most Copilot models accept only 1 image per prompt
    llm_timeout_s: float = 60.0  # per request
    llm_start_timeout_s: float = 60.0  # wait for the Copilot client to start (it takes ~15 s at launch)
    copilot_cli_path: str | None = None  # None = the SDK's own downloaded runtime
    response_font_pt: float = 13.0  # rendered markdown body size (Show Last Response / response panel)
    response_view_width_pt: float = 560.0
    response_view_max_height_pt: float = 520.0  # taller responses scroll
    response_caption_s: float = 8.0  # until Phase 13's panel: show the response's first line in the caption
    llm_prompt_template: str = (
        "I am looking at my screen. I held a hotkey, spoke an instruction, and may have circled or underlined "
        "things on screen while talking.\n\n"
        "Attached: {n_images} screenshot(s) of what I was looking at, in chronological order. Numbered boxes "
        "like [1], [2] mark what I circled or underlined. Gestures: {gestures}.\n\n"
        "My spoken instruction (transcribed; [n] marks the moment I made gesture n, so words right before "
        "a marker usually refer to that box):\n\"{transcript}\"\n\n"
        "Respond to the instruction directly and concisely. Refer to marked items by number when it helps."
    )

    # caption
    caption_hide_delay_s: float = 2.0
    # Use a fake audio processor that emits placeholder words (to exercise the caption before real STT).
    fake_transcript: bool = False

    # debugging
    debug: bool = field(default_factory=lambda: os.environ.get("STREAK_DEBUG") == "1")

    def describe(self) -> str:
        return "\n".join(f"  {f.name} = {getattr(self, f.name)!r}" for f in fields(self))


CONFIG = Config()
