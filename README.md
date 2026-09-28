# VOICEVOX × DaVinci Resolve Automation

VOICEVOX Engine API と DaVinci Resolve Scripting API を連携し、**テキストから音声生成 → タイムライン配置 → Text+字幕生成・同期**までを自動化する個人開発ツールです。

動画制作における「音声生成」「字幕入力」「タイムライン配置」といった反復作業の削減を目的に開発しました。

## Demo

![VOICEVOX × DaVinci Resolve automation demo](assets/voice-resolve-automation-demo.gif)

テキスト入力からVOICEVOXで音声を生成し、DaVinci Resolveのタイムラインへ音声とText+字幕を自動配置するデモです。

## Features

- VOICEVOX Engine API から話者 / スタイル一覧を取得
- 入力テキストから音声を自動生成
- 1行ごとに1つのWAV + 1つのText+を生成
- DaVinci Resolve のタイムラインへ音声を自動配置
- Text+ 字幕を自動生成
- 既存 Text+ のスタイルをテンプレートとしてコピー
- 音声と字幕クリップをリンク
- 再生ヘッド位置から連続配置
- 話速 / ピッチ / 抑揚 / 音量などをGUIから調整
- 設定をJSONへ保存し、次回起動時に復元
- Resolve API のバージョン差を考慮した配置補正・ログ出力

## Why I built this

ゲーム実況動画の制作時、以下の作業を繰り返す必要がありました。

1. VOICEVOXでセリフを入力
2. WAVを書き出す
3. DaVinci Resolveへ読み込む
4. タイムラインへ配置
5. Text+字幕を作成
6. 音声と字幕の位置を合わせる

このワークフローを自動化するため、VOICEVOX API と Resolve Scripting API を組み合わせたツールを開発しました。

## Architecture

```text
User
  |
  | text
  v
Python GUI
  |
  +----> VOICEVOX Engine API
  |        | /speakers
  |        | /audio_query
  |        + /synthesis
  |             |
  |             v
  |           WAV
  |
  +----> DaVinci Resolve Scripting API
           |
           +--> Media Pool
           +--> Audio Track
           +--> Fusion Text+
           +--> Clip Linking
```

## Tech Stack

- Python
- VOICEVOX Engine HTTP API
- DaVinci Resolve Scripting API
- Fusion / Text+
- tkinter
- JSON

## Requirements

- Python 3
- VOICEVOX / VOICEVOX Engine
- DaVinci Resolve 21.x
- Resolve Scripting API が利用可能な環境

> 開発・動作確認は DaVinci Resolve 21.0.4 系を中心に行っています。Resolve のバージョンやエディション、Scripting API の制約により一部挙動が異なる場合があります。

## Usage

1. VOICEVOX を起動します。
2. DaVinci Resolve で対象プロジェクト / タイムラインを開きます。
3. 必要に応じて、字幕スタイルのテンプレートにしたい Text+ をタイムラインへ配置します。
4. `src/voicevox_resolve.py` を Resolve から実行します。
5. GUIで話者、音声パラメータ、配置先トラック等を設定します。
6. 1行につき1セリフとしてテキストを入力し、「生成して配置」を実行します。

例:

```text
こんばんは
今回はエーフィを使っていきます
それでは対戦よろしくお願いします
```

上記の場合、3つの音声と3つの Text+ 字幕が順番に生成・配置されます。

## Output

生成したWAV・設定・デバッグログはデフォルトで以下へ保存します。

```text
~/Documents/ResolveVoicevoxAudio/
```

- WAVファイル
- `voicevox_resolve_config.json`
- `voicevox_resolve_debug.log`

## Current Limitations

DaVinci Resolve の公開 Scripting API には、Fusion Title の挿入先トラックや TimelineItem の尺変更に制約があります。そのため、一部の処理はベストエフォート実装です。

特に:

- Text+ の配置先は Resolve 側のターゲット / パッチ状態の影響を受けます
- Text+ タイムラインクリップの尺を直接変更できない場合があります
- Resolve バージョンによって API の戻り値に差があります

こうした差異に対応するため、タイムライン走査やログを利用して挙動を確認・補正しています。

## Development Notes

このリポジトリは、完成済み製品というよりも **動画制作ワークフローをAPI連携で自動化するPoC / 個人開発** として公開しています。

単なるツール利用ではなく、

- 外部APIとの連携
- デスクトップアプリケーションのScripting API操作
- 動画制作工程の分析
- 人間が行うべき作業と自動化可能な作業の切り分け
- API制約下でのフォールバック設計

を実際に試しながら開発しています。

## Planned Improvements

- [ ] セットアップの簡略化
- [ ] Text+ テンプレート指定方法の改善
- [ ] 字幕尺のより安定した同期
- [ ] リップシンク機能との統合
- [ ] 設定画面の改善
- [ ] デモ動画 / GIF の追加

## Disclaimer

VOICEVOX、DaVinci Resolve、および各関連製品・サービスはそれぞれの権利者に帰属します。本リポジトリは非公式の個人開発プロジェクトです。
