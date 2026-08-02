# 豊島区ごみ収集カレンダー(非公式)— Web版

GitHub Pages で公開している単一ページの Web アプリ。

## ファイル構成

| ファイル | 役割 |
|---|---|
| `toshima-gomi.json` | **収集曜日データ**(区の収集曜日一覧PDF由来)。ページ起動時に fetch で読み込む |
| `app-data.json` | 公式ごみ分別アプリWeb版のスナップショット(分別辞典774品目・FAQ・連絡先・便利帳画像) |
| `index.template.html` | ページ本体のテンプレート(CSS/JS 含む) |
| `build.py` | `index.template.html` + `app-data.json` → `index.html` を生成 |
| `index.html` | **生成物。直接編集しないこと**(build.py で上書きされる) |

## データ更新の手順

### 収集曜日データ(toshima-gomi.json)を更新する場合

1. `toshima-gomi.json` を編集する(iOS版リポジトリ `../ToshimaGomiApp/Shared/toshima-gomi.json` がマスター。更新後にこちらへコピーする)

   ```sh
   cp ../ToshimaGomiApp/Shared/toshima-gomi.json toshima-gomi.json
   ```

2. commit & push するだけ。**build.py の再実行は不要**(実行時に fetch で読むため)

### 分別辞典・連絡先など(app-data.json)やテンプレートを更新する場合

```sh
python3 build.py   # index.html を再生成
```

を実行してから commit & push する。

## ローカルでの動作確認

`index.html` をブラウザで直接開くと fetch が制限されてデータを読み込めない。
必ずローカルサーバ経由で開くこと:

```sh
python3 -m http.server 8000
# → http://localhost:8000 を開く
```

## 関連

- iOS版(SwiftUI + WidgetKit): `../ToshimaGomiApp`。データの出典・照合手順はそちらの README を参照。
