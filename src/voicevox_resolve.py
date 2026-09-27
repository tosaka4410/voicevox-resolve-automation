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
    if not imported:
        raise RuntimeError(
            "WAVは生成できましたが、Media Poolへ登録できませんでした。"
        )

    media_item = imported[0]

    try:
        actual_clip_name = media_item.GetClipProperty(
            "Clip Name"
        )
        if actual_clip_name:
            clip_name = str(actual_clip_name)
        debug_log(
            f"[IMPORT] Clip Name = {clip_name}"
        )
    except Exception:
        pass

    # Aトラック状態
    try:
        locked = timeline_global.GetIsTrackLocked(
            "audio",
            AUDIO_TRACK
        )
        enabled = timeline_global.GetIsTrackEnabled(
            "audio",
            AUDIO_TRACK
        )

        debug_log(
            f"[TRACK] A{AUDIO_TRACK}: "
            f"locked={locked}, enabled={enabled}"
        )

        if locked:
            unlock_result = timeline_global.SetTrackLock(
                "audio",
                AUDIO_TRACK,
                False
            )
            debug_log(
                f"[TRACK] unlock A{AUDIO_TRACK} -> "
                f"{unlock_result}"
            )
    except Exception as e:
        debug_log(
            f"[TRACK] audio track state check failed: {e}"
        )

    timeline_start = int(
        timeline_global.GetStartFrame()
    )

    desired_abs = int(record_frame)
    desired_rel = int(record_frame - timeline_start)

    debug_log(
        f"[POSITION] desiredAbs={desired_abs}, "
        f"timelineStart={timeline_start}, "
        f"desiredRelative={desired_rel}"
    )

    def append_at(frame_value, label):
        clip_info = {
            "mediaPoolItem": media_item,
            "mediaType": 2,
            "trackIndex": AUDIO_TRACK,
            "recordFrame": int(frame_value),
        }

        debug_log(
            f"[APPEND:{label}] request: "
            f"A{AUDIO_TRACK}, recordFrame={int(frame_value)}"
        )

        try:
            result = media_pool.AppendToTimeline(
                [clip_info]
            )
        except Exception as e:
            debug_log(
                f"[APPEND:{label}] exception: {e}"
            )
            result = None

        debug_log(
            f"[APPEND:{label}] result="
            f"{type(result).__name__}, "
            f"count={len(result) if result else 0}"
        )

        return result

    # --------------------------------------------
    # 第1候補: 従来の絶対フレーム
    # --------------------------------------------
    result = append_at(
        desired_abs,
        "ABS"
    )

    if not result:
        raise RuntimeError(
            "Media Poolへの登録は成功しましたが、"
            "AppendToTimeline()が失敗しました。"
        )

    matches = log_audio_scan(
        timeline_global,
        clip_name,
        "ABS"
    )

    # 今回生成した名前は一意なので通常1件。
    newest = matches[-1] if matches else None

    if newest and is_reasonable_start(
        newest,
        desired_abs,
        desired_rel
    ):
        debug_log(
            "[POSITION] ABS配置を採用します"
        )
        return newest["item"]

    # --------------------------------------------
    # ABSで明らかに別位置なら削除してRELを試す
    # --------------------------------------------
    if newest:
        debug_log(
            "[POSITION] ABS配置位置が期待値と一致しないため"
            "削除して相対フレーム方式を試します"
        )

        try:
            deleted = timeline_global.DeleteClips(
                [newest["item"]],
                False
            )
            debug_log(
                f"[POSITION] Delete misplaced ABS -> "
                f"{deleted}"
            )
        except Exception as e:
            debug_log(
                f"[POSITION] Delete misplaced ABS exception: {e}"
            )

    else:
        # AppendToTimelineの戻り値はあるのに走査で見つからない場合、
        # 戻り値のTimelineItemを削除してから再試行。
        try:
            deleted = timeline_global.DeleteClips(
                [result[0]],
                False
            )
            debug_log(
                f"[POSITION] Delete unlocated ABS result -> "
                f"{deleted}"
            )
        except Exception as e:
            debug_log(
                f"[POSITION] Delete unlocated ABS exception: {e}"
            )

    result_rel = append_at(
        desired_rel,
        "REL"
    )

    if not result_rel:
        raise RuntimeError(
            "絶対/相対の両方式で音声配置に失敗しました。"
        )

    rel_matches = log_audio_scan(
        timeline_global,
        clip_name,
        "REL"
    )

    rel_newest = (
        rel_matches[-1]
        if rel_matches
        else None
    )

    if rel_newest:
        debug_log(
            "[POSITION] REL配置を採用します"
        )
        return rel_newest["item"]

    # 走査APIで見えなくてもAppendToTimeline自体は成功しているので
    # 最後は戻り値を返して処理継続。
    debug_log(
        "[POSITION] 警告: タイムライン走査では検出できませんが、"
        "REL AppendToTimelineの戻り値を採用します"
    )

    return result_rel[0]


def append_text_plus_from_media_pool(
    media_pool,
    template_media_item,
    text,
    record_frame,
    duration_frames,
    video_track,
):
    """
    Media Poolに保存したText+テンプレートを、
    AppendToTimeline()で指定Vトラック・指定位置・指定尺へ配置する。

    InsertFusionTitleIntoTimeline()は使用しないため、
    既存タイトルの分割/rippleを発生させない。
    """
    duration_frames = max(
        1,
        int(round(duration_frames))
    )

    clip_info = {
        "mediaPoolItem": template_media_item,
        "startFrame": 0,
        "endFrame": duration_frames - 1,
        "mediaType": 1,  # Video
        "trackIndex": int(video_track),
        "recordFrame": int(record_frame),
    }

    debug_log(
        f"[TITLE-APPEND] request: "
        f"V{video_track}, "
        f"recordFrame={int(record_frame)}, "
        f"duration={duration_frames}"
    )

    try:
        result = media_pool.AppendToTimeline(
            [clip_info]
        )
    except Exception as e:
        debug_log(
            f"[TITLE-APPEND] exception: {e}"
        )
        result = None

    debug_log(
        f"[TITLE-APPEND] result="
        f"{type(result).__name__}, "
        f"count={len(result) if result else 0}"
    )

    if not result:
        raise RuntimeError(
            "Media PoolのText+テンプレートを"
            "タイムラインへ配置できませんでした。"
        )

    title_item = result[0]

    # Timeline上のインスタンスだけ本文を差し替える。
    changed = set_text_plus_text(
        title_item,
        text
    )

    debug_log(
        f"[TITLE-APPEND] StyledText set -> {changed}"
    )

    if not changed:
        raise RuntimeError(
            "Text+テンプレートは配置できましたが、"
            "StyledTextを書き換えられませんでした。"
        )

    return title_item


def insert_text_plus(
    resolve,
    timeline,
    text,
    audio_start,
    fps,
    template_tool
):
    """
    Text+を音声開始位置へ挿入。
    まずEditページへ切り替えてから実行する。
    """
    target_tc = frame_to_timecode(
        audio_start,
        fps
    )

    try:
        current_page = resolve.GetCurrentPage()
    except Exception:
        current_page = None

    debug_log(f"[TITLE] current page before insert = {current_page}")

    try:
        page_result = resolve.OpenPage("edit")
        debug_log(f"[TITLE] OpenPage('edit') -> {page_result}")
    except Exception as e:
        debug_log(f"[TITLE] OpenPage exception: {e}")

    try:
        tc_result = timeline.SetCurrentTimecode(target_tc)
        debug_log(
            f"[TITLE] SetCurrentTimecode({target_tc}) -> {tc_result}"
        )
    except Exception as e:
        debug_log(f"[TITLE] SetCurrentTimecode exception: {e}")

    # Vトラック状態を確認
    try:
        locked = timeline.GetIsTrackLocked("video", VIDEO_TRACK)
        enabled = timeline.GetIsTrackEnabled("video", VIDEO_TRACK)
        debug_log(
            f"[TRACK] V{VIDEO_TRACK}: locked={locked}, enabled={enabled}"
        )
        if locked:
            unlock_result = timeline.SetTrackLock("video", VIDEO_TRACK, False)
            debug_log(
                f"[TRACK] unlock V{VIDEO_TRACK} -> {unlock_result}"
            )
    except Exception as e:
        debug_log(f"[TRACK] video track state check failed: {e}")

    try:
        title_item = timeline.InsertFusionTitleIntoTimeline(
            TEXT_PLUS_NAME
        )
    except Exception as e:
        debug_log(f"[TITLE] InsertFusionTitle exception: {e}")
        title_item = None

    debug_log(
        f"[TITLE] InsertFusionTitleIntoTimeline('{TEXT_PLUS_NAME}') "
        f"-> {'OK' if title_item else 'None'}"
    )

    if not title_item:
        raise RuntimeError(
            "Text+の挿入に失敗しました。"
        )

    # Resolve 21.0.4ではGetTrackTypeAndIndex()がNoneを返す場合がある。
    # Text+オブジェクトが返ってきたことだけを成功判定に使う。
    debug_log(
        f"[TITLE] created at requested timecode = {target_tc}"
    )

    dest_tool = get_first_textplus_tool(
        title_item
    )

    if (
        template_tool is not None
        and dest_tool is not None
    ):
        copied = copy_textplus_style(
            template_tool,
            dest_tool
        )
        debug_log(f"[TITLE] style copied inputs = {copied}")

    text_result = set_text_plus_text(
        title_item,
        text
    )
    debug_log(f"[TITLE] StyledText set -> {text_result}")

    return title_item


def place_one(
    resolve,
    timeline,
    media_pool,
    text,
    speaker_id,
    speed_scale,
    pitch_scale,
    intonation_scale,
    volume_scale,
    pre_phoneme_length,
    post_phoneme_length,
    record_frame,
    fps,
    index,
    template_media_item,
    log
):
    """
    v8:
      1. VOICEVOX生成
      2. Media Pool Text+テンプレートを exact duration でVトラックへ配置
      3. 同じ開始位置へ音声配置
      4. リンク
    """
    log(
        f"生成中 {index}: {text}"
    )

    # --------------------------------------------------------
    # 1. VOICEVOX音声生成
    # --------------------------------------------------------

    wav_bytes = synthesize(
        text=text,
        speaker_id=speaker_id,
        speed_scale=speed_scale,
        pitch_scale=pitch_scale,
        intonation_scale=intonation_scale,
        volume_scale=volume_scale,
        pre_phoneme_length=pre_phoneme_length,
        post_phoneme_length=post_phoneme_length,
    )

    wav_path = save_wav(
        wav_bytes,
        text,
        index
    )

    wav_duration_seconds = (
        get_wav_duration_seconds(
            wav_path
        )
    )

    audio_duration_frames = max(
        1,
        int(round(
            wav_duration_seconds * fps
        ))
    )

    audio_start = int(record_frame)
    audio_end = (
        audio_start
        + audio_duration_frames
    )

    log(
        f"  WAV duration: "
        f"{wav_duration_seconds:.3f}s "
        f"= {audio_duration_frames} frames"
    )

    # --------------------------------------------------------
    # 2. Text+ exact append
    # --------------------------------------------------------

    title_item = (
        append_text_plus_from_media_pool(
            media_pool=media_pool,
            template_media_item=template_media_item,
            text=text,
            record_frame=audio_start,
            duration_frames=audio_duration_frames,
            video_track=VIDEO_TRACK,
        )
    )

    log(
        f"  Text+: "
        f"V{VIDEO_TRACK} "
        f"{audio_start}->{audio_end}"
    )

    # --------------------------------------------------------
    # 3. 同じ位置へ音声
    # --------------------------------------------------------

    audio_item = import_audio(
        resolve,
        media_pool,
        wav_path,
        audio_start
    )

    try:
        placed_info = inspect_timeline_item(
            audio_item
        )
        log(
            f"  Audio actual: "
            f"start={placed_info.get('start')}, "
            f"end={placed_info.get('end')}"
        )
    except Exception:
        pass

    # --------------------------------------------------------
    # 4. Link
    # --------------------------------------------------------

    try:
        linked = timeline.SetClipsLinked(
            [audio_item, title_item],
            True
        )
        log(
            f"  リンク: {linked}"
        )
    except Exception as e:
        log(
            f"  リンク失敗: {e}"
        )

    return (
        audio_end
        + int(round(
            GAP_SECONDS * fps
        ))
    )


# ============================================================
# GUI
# ============================================================

class ToolState:
    def __init__(self):
        self.speakers = []
        self.selected_index = 0
        self.text = ""
        self.speed_scale = 1.0
        self.pitch_scale = 0.0
        self.intonation_scale = 1.0
        self.volume_scale = 1.0
        self.pre_phoneme_length = 0.1
        self.post_phoneme_length = 0.1
        self.audio_track = AUDIO_TRACK
        self.video_track = VIDEO_TRACK
        self.gap_seconds = GAP_SECONDS
        self.cancelled = True


def show_tk_gui(speakers, config):
    """
    Resolve用VOICEVOX GUI。

    - 話者
    - VOICEVOX音声設定
    - 配置先A/Vトラック
    - セリフ間隔
    を編集でき、実行時に設定を保存する。
    """
    import tkinter as tk
    from tkinter import ttk
    from tkinter import messagebox

    state = ToolState()
    state.speakers = speakers

    root = tk.Tk()
    root.title("VOICEVOX → DaVinci Resolve")
    root.geometry("760x790")

    root.columnconfigure(0, weight=1)
    root.rowconfigure(11, weight=1)

    # --------------------------------------------------------
    # Speaker
    # --------------------------------------------------------

    tk.Label(
        root,
        text="話者 / スタイル"
    ).grid(
        row=0,
        column=0,
        sticky="w",
        padx=12,
        pady=(12, 4),
    )

    combo = ttk.Combobox(
        root,
        state="readonly",
        values=[
            s["display"]
            for s in speakers
        ],
    )
    combo.grid(
        row=1,
        column=0,
        sticky="ew",
        padx=12,
    )

    selected_index = 0
    saved_speaker_id = config.get(
        "speaker_id"
    )

    if saved_speaker_id is not None:
        for i, speaker in enumerate(speakers):
            if int(speaker["speaker_id"]) == int(saved_speaker_id):
                selected_index = i
                break

    if speakers:
        combo.current(selected_index)

    # --------------------------------------------------------
    # Timeline settings
    # --------------------------------------------------------

    timeline_frame = ttk.LabelFrame(
        root,
        text="タイムライン配置"
    )
    timeline_frame.grid(
        row=2,
        column=0,
        sticky="ew",
        padx=12,
        pady=(12, 6),
    )

    for c in range(6):
        timeline_frame.columnconfigure(
            c,
            weight=1 if c in (1, 3, 5) else 0
        )

    audio_track_var = tk.IntVar(
        value=int(
            config.get(
                "audio_track",
                AUDIO_TRACK
            )
        )
    )

    video_track_var = tk.IntVar(
        value=int(
            config.get(
                "video_track",
                VIDEO_TRACK
            )
        )
    )

    gap_var = tk.DoubleVar(
        value=float(
            config.get(
                "gap_seconds",
                GAP_SECONDS
            )
        )
    )

    tk.Label(
        timeline_frame,
        text="音声トラック A"
    ).grid(
        row=0, column=0,
        padx=(8, 4), pady=8,
        sticky="w"
    )

    tk.Spinbox(
        timeline_frame,
        from_=1,
        to=99,
        width=5,
        textvariable=audio_track_var,
    ).grid(
        row=0, column=1,
        padx=(0, 12),
        sticky="w"
    )

    tk.Label(
        timeline_frame,
        text="字幕トラック V"
    ).grid(
        row=0, column=2,
        padx=(8, 4), pady=8,
        sticky="w"
    )

    tk.Spinbox(
        timeline_frame,
        from_=1,
        to=99,
        width=5,
        textvariable=video_track_var,
    ).grid(
        row=0, column=3,
        padx=(0, 12),
        sticky="w"
    )

    tk.Label(
        timeline_frame,
        text="セリフ間隔(秒)"
    ).grid(
        row=0, column=4,
        padx=(8, 4), pady=8,
        sticky="w"
    )

    tk.Spinbox(
        timeline_frame,
        from_=0.0,
        to=5.0,
        increment=0.05,
        width=7,
        textvariable=gap_var,
    ).grid(
        row=0, column=5,
        padx=(0, 8),
        sticky="w"
    )

    tk.Label(
        timeline_frame,
        text=(
            "※ v8ではText+をMedia Poolテンプレートから"
            "指定Vトラックへ直接配置します。"
        ),
        justify="left",
        anchor="w",
    ).grid(
        row=1,
        column=0,
        columnspan=6,
        sticky="ew",
        padx=8,
        pady=(0, 8),
    )

    # --------------------------------------------------------
    # Voice settings
    # --------------------------------------------------------

    settings = ttk.LabelFrame(
        root,
        text="VOICEVOX 音声設定"
    )
    settings.grid(
        row=3,
        column=0,
        sticky="ew",
        padx=12,
        pady=6,
    )
    settings.columnconfigure(1, weight=1)

    controls = [
        (
            "話速",
            "speed",
            0.5,
            2.0,
            float(config.get("speed_scale", 1.0)),
            0.01,
        ),
        (
            "音高",
            "pitch",
            -0.15,
            0.15,
            float(config.get("pitch_scale", 0.0)),
            0.001,
        ),
        (
            "抑揚",
            "intonation",
            0.0,
            2.0,
            float(config.get("intonation_scale", 1.0)),
            0.01,
        ),
        (
            "音量",
            "volume",
            0.0,
            2.0,
            float(config.get("volume_scale", 1.0)),
            0.01,
        ),
        (
            "開始無音(秒)",
            "pre",
            0.0,
            1.0,
            float(config.get("pre_phoneme_length", 0.1)),
            0.01,
        ),
        (
            "終了無音(秒)",
            "post",
            0.0,
            1.0,
            float(config.get("post_phoneme_length", 0.1)),
            0.01,
        ),
    ]

    vars_ = {}

    for row, (
        label,
        key,
        vmin,
        vmax,
        default,
        resolution,
    ) in enumerate(controls):

        tk.Label(
            settings,
            text=label,
            width=14,
            anchor="w",
        ).grid(
            row=row,
            column=0,
            sticky="w",
            padx=(8, 4),
            pady=3,
        )

        var = tk.DoubleVar(
            value=default
        )
        vars_[key] = var

        scale = tk.Scale(
            settings,
            from_=vmin,
            to=vmax,
            resolution=resolution,
            orient="horizontal",
            variable=var,
            showvalue=False,
        )
        scale.grid(
            row=row,
            column=1,
            sticky="ew",
            padx=4,
        )

        tk.Label(
            settings,
            textvariable=var,
            width=8,
            anchor="e",
        ).grid(
            row=row,
            column=2,
            padx=(4, 8),
        )

    # --------------------------------------------------------
    # Text input
    # --------------------------------------------------------

    tk.Label(
        root,
        text=(
            "セリフ "
            "（1行 = 1音声 + 1Text+）"
        ),
    ).grid(
        row=10,
        column=0,
        sticky="w",
        padx=12,
        pady=(8, 4),
    )

    text_box = tk.Text(
        root,
        wrap="word",
    )
    text_box.grid(
        row=11,
        column=0,
        sticky="nsew",
        padx=12,
    )

    info_var = tk.StringVar(
        value=(
            f"生成WAV: {OUTPUT_DIR}\n"
            f"設定保存: {CONFIG_PATH}"
        )
    )

    tk.Label(
        root,
        textvariable=info_var,
        justify="left",
    ).grid(
        row=12,
        column=0,
        sticky="w",
        padx=12,
        pady=8,
    )

    button_frame = tk.Frame(root)
    button_frame.grid(
        row=13,
        column=0,
        sticky="e",
        padx=12,
        pady=(0, 12),
    )

    def on_generate():
        raw = text_box.get(
            "1.0",
            "end"
        ).strip()

        if not raw:
            messagebox.showwarning(
                "VOICEVOX",
                "セリフを入力してください。",
            )
            return

        try:
            audio_track = int(
                audio_track_var.get()
            )
            video_track = int(
                video_track_var.get()
            )
            gap = float(
                gap_var.get()
            )
        except Exception:
            messagebox.showwarning(
                "VOICEVOX",
                "トラック番号と間隔を確認してください。",
            )
            return

        if (
            audio_track < 1
            or video_track < 1
            or gap < 0
        ):
            messagebox.showwarning(
                "VOICEVOX",
                "トラック番号は1以上、"
                "間隔は0以上にしてください。",
            )
            return

        state.selected_index = max(
            0,
            combo.current()
        )
        state.text = raw

        state.speed_scale = float(
            vars_["speed"].get()
        )
        state.pitch_scale = float(
            vars_["pitch"].get()
        )
        state.intonation_scale = float(
            vars_["intonation"].get()
        )
        state.volume_scale = float(
            vars_["volume"].get()
        )
        state.pre_phoneme_length = float(
            vars_["pre"].get()
        )
        state.post_phoneme_length = float(
            vars_["post"].get()
        )

        state.audio_track = audio_track
        state.video_track = video_track
        state.gap_seconds = gap
        state.cancelled = False
        root.destroy()

    def on_reset():
        defaults = default_config()

        vars_["speed"].set(
            defaults["speed_scale"]
        )
        vars_["pitch"].set(
            defaults["pitch_scale"]
        )
        vars_["intonation"].set(
            defaults["intonation_scale"]
        )
        vars_["volume"].set(
            defaults["volume_scale"]
        )
        vars_["pre"].set(
            defaults["pre_phoneme_length"]
        )
        vars_["post"].set(
            defaults["post_phoneme_length"]
        )

        audio_track_var.set(
            defaults["audio_track"]
        )
        video_track_var.set(
            defaults["video_track"]
        )
        gap_var.set(
            defaults["gap_seconds"]
        )

    def on_cancel():
        state.cancelled = True
        root.destroy()

    tk.Button(
        button_frame,
        text="初期値に戻す",
        command=on_reset,
    ).pack(
        side="left",
        padx=(0, 16)
    )

    tk.Button(
        button_frame,
        text="キャンセル",
        command=on_cancel,
    ).pack(
        side="right",
        padx=(8, 0)
    )

    tk.Button(
        button_frame,
        text="生成して配置",
        command=on_generate,
    ).pack(
        side="right"
    )

    root.mainloop()
    return state


# ============================================================
# メイン
# ============================================================

def main():
    print("")
    print("=" * 60)
    print("VOICEVOX -> DaVinci Resolve")
    print("=" * 60)

    # --------------------------------------------------------
    # VOICEVOX接続
    # --------------------------------------------------------

    try:
        speakers = get_speakers()
    except urllib.error.URLError as e:
        print(
            "[FATAL] VOICEVOX Engineへ"
            "接続できません"
        )
        print(
            f"URL: {VOICEVOX_URL}"
        )
        print(
            "VOICEVOXまたはEngineを"
            "起動してから再実行してください。"
        )
        print(e)
        return
    except Exception as e:
        print(
            "[FATAL] VOICEVOX APIエラー:"
        )
        print(e)
        return

    if not speakers:
        print(
            "[FATAL] 利用可能なtalk話者が"
            "見つかりません"
        )
        return

    print(
        f"[INFO] VOICEVOX話者:"
        f" {len(speakers)}スタイル"
    )

    # --------------------------------------------------------
    # Resolve
    # --------------------------------------------------------

    try:
        resolve = get_resolve()
    except Exception as e:
        print("[FATAL]", e)
        return

    project = (
        resolve.GetProjectManager()
        .GetCurrentProject()
    )

    if not project:
        print(
            "[FATAL] プロジェクトが"
            "開かれていません"
        )
        return

    timeline = (
        project.GetCurrentTimeline()
    )

    global timeline_global
    timeline_global = timeline

    if not timeline:
        print(
            "[FATAL] タイムラインが"
            "開かれていません"
        )
        return

    media_pool = project.GetMediaPool()

    try:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        LOG_PATH.write_text("", encoding="utf-8")
    except Exception:
        pass

    try:
        debug_log(f"[RESOLVE] product = {resolve.GetProductName()}")
        debug_log(f"[RESOLVE] version = {resolve.GetVersionString()}")
        debug_log(f"[RESOLVE] current page = {resolve.GetCurrentPage()}")
        debug_log(f"[RESOLVE] timeline = {timeline.GetName()}")
        debug_log(
            f"[RESOLVE] fps = {timeline.GetSetting('timelineFrameRate')}"
        )
        debug_log(
            f"[RESOLVE] currentTC = {timeline.GetCurrentTimecode()}"
        )
        try:
            debug_log(
                f"[RESOLVE] startTC = {timeline.GetStartTimecode()}"
            )
        except Exception:
            pass
        debug_log(
            f"[RESOLVE] startFrame = {timeline.GetStartFrame()}"
        )
        debug_log(
            f"[RESOLVE] tracks: video={timeline.GetTrackCount('video')}, "
            f"audio={timeline.GetTrackCount('audio')}"
        )
    except Exception as e:
        debug_log(f"[RESOLVE] environment inspection failed: {e}")

    # --------------------------------------------------------
    # GUI
    # --------------------------------------------------------

    config = load_config()

    try:
        state = show_tk_gui(
            speakers,
            config
        )
    except Exception as e:
        print(
            "[FATAL] 入力GUIを"
            "開けませんでした"
        )
        print(e)
        print(
            "この環境でtkinterが使えない場合は"
            "Fusion UIManager版へ切替が必要です。"
        )
        return

    if state.cancelled:
        print("[INFO] キャンセル")
        return

    # GUIで選ばれた設定を、この実行中のグローバル設定として反映。
    global AUDIO_TRACK
    global VIDEO_TRACK
    global GAP_SECONDS

    AUDIO_TRACK = int(
        state.audio_track
    )
    VIDEO_TRACK = int(
        state.video_track
    )
    GAP_SECONDS = float(
        state.gap_seconds
    )

    try:
        ensure_tracks(timeline)
    except Exception as e:
        print("[FATAL]", e)
        return

    speaker = speakers[
        state.selected_index
    ]

    save_config(
        state,
        speaker["speaker_id"]
    )

    # 空行は無視
    lines = [
        line.strip()
        for line in state.text.splitlines()
        if line.strip()
    ]

    if not lines:
        print(
            "[INFO] 有効なセリフが"
            "ありません"
        )
        return

    print(
        "[INFO] 話者:",
        speaker["display"],
        f"(id={speaker['speaker_id']})",
    )
    print(
        "[INFO] セリフ:",
        len(lines),
        "件"
    )

    for i, parsed_line in enumerate(
        lines,
        start=1
    ):
        print(
            f"[INPUT] {i}: {parsed_line}"
        )
    print(
        "[INFO] 配置設定:",
        f"A{AUDIO_TRACK},",
        f"V{VIDEO_TRACK},",
        f"gap={GAP_SECONDS:.2f}s",
    )
    print(
        "[INFO] 音声設定:",
        f"speed={state.speed_scale:.2f},",
        f"pitch={state.pitch_scale:.3f},",
        f"intonation={state.intonation_scale:.2f},",
        f"volume={state.volume_scale:.2f},",
        f"pre={state.pre_phoneme_length:.2f},",
        f"post={state.post_phoneme_length:.2f}",
    )

    # --------------------------------------------------------
    # Text+ Media Poolテンプレート
    # --------------------------------------------------------

    template_media_item = (
        find_text_template_media_item(
            media_pool
        )
    )

    if not template_media_item:
        print("")
        print("=" * 70)
        print(
            f"[FATAL] Media Poolに "
            f"'{TEXT_TEMPLATE_CLIP_NAME}' "
            "が見つかりません。"
        )
        print("")
        print(
            "普段使っているText+をタイムラインに1つ置き、"
            "そのText+をMedia Poolへドラッグして保存してください。"
        )
        print(
            f"保存したクリップ名を "
            f"'{TEXT_TEMPLATE_CLIP_NAME}' "
            "に変更してください。"
        )
        print("")
        print(
            "この方法にすると、書式を完全に維持したまま"
            "字幕のVトラック・開始位置・尺を指定できます。"
        )
        print("=" * 70)
        return

    try:
        template_name = template_media_item.GetClipProperty(
            "Clip Name"
        )
    except Exception:
        template_name = TEXT_TEMPLATE_CLIP_NAME

    print(
        f"[INFO] Text+ Media Pool template: "
        f"{template_name}"
    )

    # --------------------------------------------------------
    # 配置開始位置
    # --------------------------------------------------------

    try:
        fps = float(
            timeline.GetSetting(
                "timelineFrameRate"
            )
        )
    except Exception:
        print(
            "[FATAL] FPSを"
            "取得できません"
        )
        return

    original_tc = (
        timeline.GetCurrentTimecode()
    )

    try:
        record_frame = (
            get_current_record_frame(
                timeline,
                fps
            )
        )
    except Exception as e:
        print(
            "[FATAL] 再生ヘッド位置を"
            "取得できません:",
            e,
        )
        return

    # --------------------------------------------------------
    # 1行ずつ生成 + 配置
    # --------------------------------------------------------

    def log(msg):
        debug_log(msg)

    success = 0

    for index, text in enumerate(
        lines,
        start=1
    ):
        try:
            record_frame = place_one(
                resolve=resolve,
                timeline=timeline,
                media_pool=media_pool,
                text=text,
                speaker_id=(
                    speaker["speaker_id"]
                ),
                speed_scale=(
                    state.speed_scale
                ),
                pitch_scale=(
                    state.pitch_scale
                ),
                intonation_scale=(
                    state.intonation_scale
                ),
                volume_scale=(
                    state.volume_scale
                ),
                pre_phoneme_length=(
                    state.pre_phoneme_length
                ),
                post_phoneme_length=(
                    state.post_phoneme_length
                ),
                record_frame=record_frame,
                fps=fps,
                index=index,
                template_media_item=template_media_item,
                log=log,
            )
            success += 1

        except urllib.error.URLError as e:
            print(
                f"[ERROR] {index}行目 "
                f"VOICEVOX通信失敗: {e}"
            )
            break

        except Exception as e:
            # 失敗時も、それ以前に生成済みの
            # クリップは絶対に削除しない
            print(
                f"[ERROR] {index}行目: {e}"
            )
            break

    # 再生ヘッドを元へ戻す
    try:
        timeline.SetCurrentTimecode(
            original_tc
        )
    except Exception:
        pass

    print("")
    print("=" * 60)
    print(
        f"完了: {success}/{len(lines)}"
    )
    print(
        f"VOICEVOX: {speaker['display']}"
    )
    print(
        f"音声トラック: A{AUDIO_TRACK}"
    )
    print(
        f"Text+配置先: V{VIDEO_TRACK}"
    )
    print(
        f"WAV保存先: {OUTPUT_DIR}"
    )
    print(
        f"デバッグログ: {LOG_PATH}"
    )
    print(
        f"設定ファイル: {CONFIG_PATH}"
    )
    print("=" * 60)


if __name__ == "__main__":
    main()