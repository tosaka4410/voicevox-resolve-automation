# -*- coding: utf-8 -*-
"""
VOICEVOX -> DaVinci Resolve 直接入力ツール v8 media-pool-template

VOICEVOX Engine APIをDaVinci Resolve側から呼び出し、
入力した文章を「音声 + Text+」としてタイムラインへ配置します。

前提:
- VOICEVOX / VOICEVOX Engine が起動していること
- 標準では http://127.0.0.1:50021 を使用
- 1行 = 1音声 = 1Text+
- 音声はAUDIO_TRACKへ配置
- Text+はResolveの現在のタイトル挿入先トラックへ配置
- VIDEO_TRACK上に既存Text+があれば、その書式を可能な範囲でコピー
- 生成した音声とText+はSetClipsLinked()でリンク

重要:
InsertFusionTitleIntoTimeline("Text+") にはtrackIndex指定がないため、
Text+の挿入先はResolve側のターゲット/パッチ状態に依存します。
VIDEO_TRACKは主に「書式テンプレートを探すトラック」として使用します。

生成WAVはメディアソースとして必要なので削除しません。
~/Documents/ResolveVoicevoxAudio/ に保存します。
"""

import os
import re
import json
import time
import urllib.parse
import urllib.request
import urllib.error
import wave
from pathlib import Path


# ============================================================
# 設定
# ============================================================

VOICEVOX_URL = "http://127.0.0.1:50021"

AUDIO_TRACK = 2
VIDEO_TRACK = 6
GAP_SECONDS = 0.15

TEXT_PLUS_NAME = "Text+"
TEXT_TEMPLATE_CLIP_NAME = "VOICEVOX_TEXT_TEMPLATE"

# V6等に置いた既存Text+のスタイルをコピーする
USE_EXISTING_TEXTPLUS_AS_TEMPLATE = True

# Resolveプロジェクトが参照し続けるため、WAVは永続保存
OUTPUT_DIR = Path.home() / "Documents" / "ResolveVoicevoxAudio"
LOG_PATH = OUTPUT_DIR / "voicevox_resolve_debug.log"
CONFIG_PATH = OUTPUT_DIR / "voicevox_resolve_config.json"


def debug_log(message):
    """
    コンソールとログファイルの両方へ記録。
    """
    msg = str(message)
    print(msg)

    try:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(msg + "\n")
    except Exception:
        pass


def default_config():
    return {
        "speaker_id": None,
        "speed_scale": 1.0,
        "pitch_scale": 0.0,
        "intonation_scale": 1.0,
        "volume_scale": 1.0,
        "pre_phoneme_length": 0.1,
        "post_phoneme_length": 0.1,
        "audio_track": AUDIO_TRACK,
        "video_track": VIDEO_TRACK,
        "gap_seconds": GAP_SECONDS,
    }


def load_config():
    cfg = default_config()

    try:
        if CONFIG_PATH.exists():
            loaded = json.loads(
                CONFIG_PATH.read_text(encoding="utf-8")
            )
            if isinstance(loaded, dict):
                cfg.update(loaded)
    except Exception as e:
        debug_log(f"[CONFIG] load failed: {e}")

    return cfg


def save_config(state, speaker_id):
    cfg = {
        "speaker_id": int(speaker_id),
        "speed_scale": float(state.speed_scale),
        "pitch_scale": float(state.pitch_scale),
        "intonation_scale": float(state.intonation_scale),
        "volume_scale": float(state.volume_scale),
        "pre_phoneme_length": float(state.pre_phoneme_length),
        "post_phoneme_length": float(state.post_phoneme_length),
        "audio_track": int(state.audio_track),
        "video_track": int(state.video_track),
        "gap_seconds": float(state.gap_seconds),
    }

    try:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(
            json.dumps(
                cfg,
                ensure_ascii=False,
                indent=2
            ),
            encoding="utf-8"
        )
        debug_log(
            f"[CONFIG] saved: {CONFIG_PATH}"
        )
    except Exception as e:
        debug_log(f"[CONFIG] save failed: {e}")


# ============================================================
# HTTP / VOICEVOX API
# ============================================================

def http_json(method, path, params=None, body=None, timeout=30):
    url = VOICEVOX_URL.rstrip("/") + path

    if params:
        url += "?" + urllib.parse.urlencode(params)

    data = None
    headers = {}

    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    elif method.upper() == "POST":
        data = b""

    req = urllib.request.Request(
        url,
        data=data,
        headers=headers,
        method=method.upper(),
    )

    with urllib.request.urlopen(req, timeout=timeout) as res:
        raw = res.read()

    if not raw:
        return None

    return json.loads(raw.decode("utf-8"))


def get_speakers():
    """
    /speakers から「キャラクター / スタイル」を取得。
    talk系スタイルを中心に一覧化する。
    """
    speakers = http_json("GET", "/speakers", timeout=10)

    result = []

    for speaker in speakers or []:
        speaker_name = speaker.get("name", "Unknown")

        for style in speaker.get("styles", []):
            style_type = style.get("type")

            # typeが無い旧Engineも許可。
            # talk以外(歌唱等)は通常の /synthesis 対象から除外。
            if style_type not in (None, "talk"):
                continue

            style_id = style.get("id")
            style_name = style.get("name", "")

            if style_id is None:
                continue

            display = (
                f"{speaker_name} / {style_name}"
                if style_name
                else speaker_name
            )

            result.append({
                "display": display,
                "speaker_id": int(style_id),
                "speaker_name": speaker_name,
                "style_name": style_name,
            })

    return result


def synthesize(
    text,
    speaker_id,
    speed_scale=1.0,
    pitch_scale=0.0,
    intonation_scale=1.0,
    volume_scale=1.0,
    pre_phoneme_length=0.1,
    post_phoneme_length=0.1,
):
    """
    /audio_query -> /synthesis
    戻り値: WAV bytes

    VOICEVOX AudioQuery の主要パラメータを
    Resolve側GUIから調整できるようにする。
    """
    query = http_json(
        "POST",
        "/audio_query",
        params={
            "text": text,
            "speaker": int(speaker_id),
        },
        timeout=30,
    )

    # Resolve側UIの音声設定をAudioQueryへ反映
    query["speedScale"] = float(speed_scale)
    query["pitchScale"] = float(pitch_scale)
    query["intonationScale"] = float(intonation_scale)
    query["volumeScale"] = float(volume_scale)
    query["prePhonemeLength"] = max(0.0, float(pre_phoneme_length))
    query["postPhonemeLength"] = max(0.0, float(post_phoneme_length))

    url = (
        VOICEVOX_URL.rstrip("/")
        + "/synthesis?"
        + urllib.parse.urlencode({
            "speaker": int(speaker_id)
        })
    )

    data = json.dumps(
        query,
        ensure_ascii=False
    ).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
        },
        method="POST",
    )

    with urllib.request.urlopen(
        req,
        timeout=120
    ) as res:
        return res.read()


# ============================================================
# Resolve 接続
# ============================================================

def get_resolve():
    try:
        r = globals().get("resolve")
        if r:
            return r
    except Exception:
        pass

    try:
        import DaVinciResolveScript as dvr_script
        return dvr_script.scriptapp("Resolve")
    except Exception as e:
        raise RuntimeError(
            "DaVinci Resolveへ接続できません: "
            + str(e)
        )


# ============================================================
# タイムコード
# ============================================================

def nominal_fps(fps):
    return int(round(float(fps)))


def timecode_to_frame(tc, fps):
    """
    Resolveのタイムライン内相対位置算出用。
    29.97/59.94 DFの厳密絶対値ではなく、
    start/currentの差分計算を主用途とする。
    """
    tc = tc.replace(";", ":")
    parts = tc.split(":")

    if len(parts) != 4:
        raise ValueError(
            "タイムコードを解釈できません: "
            + tc
        )

    hh, mm, ss, ff = [
        int(x) for x in parts
    ]

    nfps = nominal_fps(fps)

    return (
        ((hh * 60 + mm) * 60 + ss)
        * nfps
        + ff
    )


def frame_to_timecode(frame, fps):
    nfps = nominal_fps(fps)
    frame = max(0, int(round(frame)))

    ff = frame % nfps
    total_seconds = frame // nfps

    ss = total_seconds % 60
    total_minutes = total_seconds // 60
    mm = total_minutes % 60
    hh = total_minutes // 60

    return (
        f"{hh:02d}:{mm:02d}:"
        f"{ss:02d}:{ff:02d}"
    )


def get_current_record_frame(timeline, fps):
    current_tc = timeline.GetCurrentTimecode()
    timeline_start_frame = timeline.GetStartFrame()

    try:
        start_tc = timeline.GetStartTimecode()
    except Exception:
        start_tc = None

    if start_tc:
        current_abs = timecode_to_frame(
            current_tc, fps
        )
        start_abs = timecode_to_frame(
            start_tc, fps
        )

        return int(
            timeline_start_frame
            + (current_abs - start_abs)
        )

    return int(
        timecode_to_frame(current_tc, fps)
    )


def find_media_pool_item_recursive(folder, target_name):
    """
    Media Pool全フォルダを再帰検索して、指定Clip NameのItemを返す。
    """
    if not folder:
        return None

    try:
        clips = folder.GetClipList() or []
    except Exception:
        clips = []

    for item in clips:
        try:
            clip_name = item.GetClipProperty("Clip Name")
        except Exception:
            clip_name = None

        if clip_name == target_name:
            return item

    try:
        subfolders = folder.GetSubFolderList() or []
    except Exception:
        subfolders = []

    for sub in subfolders:
        found = find_media_pool_item_recursive(
            sub,
            target_name
        )
        if found:
            return found

    return None


def find_text_template_media_item(media_pool):
    """
    Media Pool内のVOICEVOX_TEXT_TEMPLATEを探す。
    """
    try:
        root = media_pool.GetRootFolder()
    except Exception:
        root = None

    return find_media_pool_item_recursive(
        root,
        TEXT_TEMPLATE_CLIP_NAME
    )


# ============================================================
# Text+ 操作
# ============================================================

def get_first_textplus_tool(timeline_item):
    if not timeline_item:
        return None

    try:
        if timeline_item.GetFusionCompCount() < 1:
            return None

        comp = timeline_item.GetFusionCompByIndex(1)
    except Exception:
        return None

    if not comp:
        return None

    try:
        tools = comp.GetToolList(False, "TextPlus")
    except Exception:
        tools = {}

    if isinstance(tools, dict) and tools:
        return next(iter(tools.values()))

    return None


def find_template_textplus(timeline, track_index):
    """
    VIDEO_TRACK上で最初に見つかったText+を
    書式テンプレートとして使う。
    """
    try:
        items = (
            timeline.GetItemListInTrack(
                "video",
                track_index
            )
            or []
        )
    except Exception:
        items = []

    for item in items:
        tool = get_first_textplus_tool(item)

        if tool:
            return item, tool

    return None, None


def _get_input_id(input_obj, fallback_key=None):
    """
    Fusion Inputオブジェクトから実際の入力ID(INPS_ID)を取得。
    """
    try:
        attrs = input_obj.GetAttrs()
        if isinstance(attrs, dict):
            input_id = attrs.get("INPS_ID")
            if input_id:
                return str(input_id)
    except Exception:
        pass

    if isinstance(fallback_key, str):
        return fallback_key

    return None


def copy_textplus_style(source_tool, dest_tool):
    """
    既存Text+の主要パラメータを新規Text+へコピーする。

    v5までの実装ではGetInputList()のキーをそのまま入力IDとして
    扱っていたため、Resolve 21では0件コピーになるケースがあった。
    この版では各InputオブジェクトのINPS_IDを取得してコピーする。

    StyledTextだけは後でVOICEVOX本文へ差し替えるため除外。
    """
    if not source_tool or not dest_tool:
        return 0

    copied = 0
    failed = 0

    try:
        inputs = source_tool.GetInputList()
    except Exception as e:
        debug_log(f"[STYLE] GetInputList exception: {e}")
        inputs = {}

    if isinstance(inputs, dict):
        for key, input_obj in inputs.items():
            input_id = _get_input_id(
                input_obj,
                fallback_key=key
            )

            if not input_id:
                continue

            if input_id == "StyledText":
                continue

            try:
                value = source_tool.GetInput(input_id)
            except Exception:
                try:
                    value = input_obj[0]
                except Exception:
                    failed += 1
                    continue

            if value is None:
                continue

            try:
                dest_tool.SetInput(input_id, value)
                copied += 1
            except Exception:
                failed += 1

    # GetInputListで拾えない/コピーできない環境向けに、
    # Text+で頻出する主要項目を明示的にも試す。
    common_ids = [
        "Font",
        "Style",
        "Size",
        "VerticalJustificationNew",
        "HorizontalJustificationNew",
        "Center",
        "LayoutType",
        "CharacterSpacing",
        "LineSpacing",
        "Red1",
        "Green1",
        "Blue1",
        "Alpha1",
        "Enabled1",
        "Thickness2",
        "Red2",
        "Green2",
        "Blue2",
        "Alpha2",
        "Enabled2",
        "Thickness3",
        "Red3",
        "Green3",
        "Blue3",
        "Alpha3",
        "Enabled3",
        "SoftnessX",
        "SoftnessY",
        "TransformRotation",
        "TransformSize",
        "TransformShear",
        "TransformAspect",
    ]

    already = set()

    try:
        dest_inputs = dest_tool.GetInputList()
        if isinstance(dest_inputs, dict):
            for key, inp in dest_inputs.items():
                iid = _get_input_id(inp, key)
                if iid:
                    already.add(iid)
    except Exception:
        pass

    for input_id in common_ids:
        try:
            value = source_tool.GetInput(input_id)
        except Exception:
            continue

        if value is None:
            continue

        try:
            dest_tool.SetInput(input_id, value)
            copied += 1
        except Exception:
            pass

    debug_log(
        f"[STYLE] copied={copied}, failed={failed}"
    )
    return copied


def set_text_plus_text(timeline_item, text):
    tool = get_first_textplus_tool(
        timeline_item
    )

    if not tool:
        return False

    try:
        tool.SetInput(
            "StyledText",
            text
        )
        return True
    except Exception:
        pass

    try:
        tool.StyledText = text
        return True
    except Exception:
        return False


def match_textplus_fusion_range(title_item, audio_duration_frames):
    """
    Resolveの公開Scripting APIにはTimelineItemの終了位置を
    直接設定するSetEnd/SetDuration相当が無いため、
    Text+クリップそのもののタイムライン尺は変更できない。

    代わりにFusion Composition内部のGlobalEndを
    音声尺へ合わせるベストエフォート処理を行う。
    Resolve/Fusionバージョンによっては効かない場合がある。

    戻り値:
        True  : Fusion内部範囲の設定に成功
        False : 設定できなかった
    """
    if not title_item:
        return False

    try:
        comp = title_item.GetFusionCompByIndex(1)
    except Exception:
        comp = None

    if not comp:
        return False

    duration = max(1, int(round(audio_duration_frames)))

    # Fusion compositionは0始まりとして扱う
    attrs_candidates = [
        {"COMPN_GlobalStart": 0, "COMPN_GlobalEnd": duration - 1},
        {"COMPN_RenderStart": 0, "COMPN_RenderEnd": duration - 1},
    ]

    for attrs in attrs_candidates:
        try:
            result = comp.SetAttrs(attrs)
            if result is not False:
                return True
        except Exception:
            continue

    return False


def get_track_info(item):
    try:
        info = item.GetTrackTypeAndIndex()

        if isinstance(
            info,
            (list, tuple)
        ) and len(info) >= 2:
            return (
                str(info[0]),
                int(info[1])
            )

        if isinstance(info, dict):
            track_type = (
                info.get("trackType")
                or info.get("type")
            )
            track_index = (
                info.get("trackIndex")
                or info.get("index")
            )

            if (
                track_type is not None
                and track_index is not None
            ):
                return (
                    str(track_type),
                    int(track_index)
                )
    except Exception:
        pass

    return None, None


# ============================================================
# WAV保存
# ============================================================

def safe_filename(text, max_len=24):
    text = re.sub(
        r'[\\/:*?"<>|\r\n\t]+',
        "_",
        text
    ).strip()

    if not text:
        text = "voice"

    return text[:max_len]


def get_wav_duration_seconds(wav_path):
    """
    ResolveのTimelineItem情報に依存せず、
    生成済みWAVそのものから正確な長さを取得する。
    """
    with wave.open(str(wav_path), "rb") as wf:
        frames = wf.getnframes()
        rate = wf.getframerate()

        if rate <= 0:
            raise RuntimeError("WAVのサンプルレートが不正です")

        return frames / float(rate)


def save_wav(wav_bytes, text, index):
    OUTPUT_DIR.mkdir(
        parents=True,
        exist_ok=True
    )

    stamp = time.strftime(
        "%Y%m%d_%H%M%S"
    )

    name = (
        f"{stamp}_{index:03d}_"
        f"{safe_filename(text)}.wav"
    )

    path = OUTPUT_DIR / name
    path.write_bytes(wav_bytes)

    return str(path)


# ============================================================
# タイムライン配置
# ============================================================

def ensure_tracks(timeline):
    while (
        timeline.GetTrackCount("audio")
        < AUDIO_TRACK
    ):
        if not timeline.AddTrack("audio"):
            raise RuntimeError(
                f"A{AUDIO_TRACK}を"
                "作成できません"
            )

    while (
        timeline.GetTrackCount("video")
        < VIDEO_TRACK
    ):
        if not timeline.AddTrack("video"):
            raise RuntimeError(
                f"V{VIDEO_TRACK}を"
                "作成できません"
            )


def timeline_item_clip_name(item):
    """
    TimelineItemから元クリップ名を可能な方法で取得。
    """
    try:
        name = item.GetName()
        if name:
            return str(name)
    except Exception:
        pass

    try:
        mpi = item.GetMediaPoolItem()
        if mpi:
            name = mpi.GetClipProperty("Clip Name")
            if name:
                return str(name)
    except Exception:
        pass

    return ""


def inspect_timeline_item(item):
    """
    Resolve 21のAPI差を吸収しながら位置情報を取得する。
    """
    info = {
        "name": timeline_item_clip_name(item),
        "start": None,
        "start_sub": None,
        "end": None,
        "end_sub": None,
        "duration": None,
    }

    for key, method_name, args in [
        ("start", "GetStart", ()),
        ("start_sub", "GetStart", (True,)),
        ("end", "GetEnd", ()),
        ("end_sub", "GetEnd", (True,)),
        ("duration", "GetDuration", ()),
    ]:
        try:
            method = getattr(item, method_name)
            value = method(*args)
            if value is not None:
                info[key] = float(value)
        except Exception:
            pass

    return info


def find_audio_clip_on_timeline(timeline, clip_name):
    """
    全オーディオトラックを走査し、clip_nameと一致するTimelineItemを探す。
    新しく生成するWAV名は一意なので、名前一致で特定できる。
    """
    found = []

    track_count = timeline.GetTrackCount("audio")

    for track_index in range(1, track_count + 1):
        try:
            items = timeline.GetItemListInTrack(
                "audio",
                track_index
            ) or []
        except Exception:
            items = []

        for item in items:
            name = timeline_item_clip_name(item)

            if name == clip_name:
                info = inspect_timeline_item(item)
                info["track"] = track_index
                info["item"] = item
                found.append(info)

    return found


def log_audio_scan(timeline, clip_name, label):
    matches = find_audio_clip_on_timeline(
        timeline,
        clip_name
    )

    debug_log(
        f"[SCAN:{label}] '{clip_name}' matches={len(matches)}"
    )

    for m in matches:
        debug_log(
            f"[SCAN:{label}] "
            f"A{m['track']} "
            f"start={m['start']} "
            f"startSub={m['start_sub']} "
            f"end={m['end']} "
            f"endSub={m['end_sub']} "
            f"duration={m['duration']}"
        )

    return matches


def is_reasonable_start(match, desired_abs, desired_rel):
    """
    Resolve APIは位置の返し方にバージョン差があるため、
    絶対表現(desired_abs)と相対表現(desired_rel)の両方を許容。
    """
    values = [
        match.get("start"),
        match.get("start_sub"),
    ]

    targets = [
        float(desired_abs),
        float(desired_rel),
    ]

    for value in values:
        if value is None:
            continue

        for target in targets:
            if abs(float(value) - target) <= 1.0:
                return True

    return False


def import_audio(
    resolve,
    media_pool,
    wav_path,
    record_frame
):
    """
    Resolve 21.0.4向け自動補正版。

    recordFrameの扱いがタイムライン開始TCによって曖昧なため、
    まず従来値で配置し、タイムラインを実際に走査して確認。
    必要なら先頭相対フレーム値で自動再試行する。
    """
    wav_path = str(Path(wav_path).resolve())
    clip_name = Path(wav_path).name

    debug_log(f"[IMPORT] WAV path = {wav_path}")
    debug_log(f"[IMPORT] exists = {os.path.exists(wav_path)}")
    debug_log(
        f"[IMPORT] size = "
        f"{os.path.getsize(wav_path) if os.path.exists(wav_path) else -1}"
    )

    imported = None

    try:
        media_storage = resolve.GetMediaStorage()
        imported = media_storage.AddItemListToMediaPool(
            [wav_path]
        )
        debug_log(
            f"[IMPORT] MediaStorage.AddItemListToMediaPool -> "
            f"{type(imported).__name__}, "
            f"count={len(imported) if imported else 0}"
        )
    except Exception as e:
        debug_log(
            f"[IMPORT] MediaStorage method exception: {e}"
        )
        imported = None

    if not imported:
        try:
            imported = media_pool.ImportMedia([wav_path])
            debug_log(
                f"[IMPORT] MediaPool.ImportMedia -> "
                f"{type(imported).__name__}, "
                f"count={len(imported) if imported else 0}"
            )
        except Exception as e:
            debug_log(
                f"[IMPORT] ImportMedia exception: {e}"
            )
            imported = None