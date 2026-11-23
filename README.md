# 豊島区ごみ収集カレンダー(非公式)— Web版

GitHub Pages で公開している単一ページの Web アプリ。

このフォルダは、iOS版と合わせて管理している本体リポジトリ(非公開)の `web/` にあたる。
公開リポジトリ `toshima-gomi` はその内容を反映したもので、直接は編集しない。
変更は本体リポジトリの `web/` で行い、`scripts/deploy-web.sh` で公開する
(本体リポジトリの README「Web版の公開」)。以下の `../` は本体リポジトリの直下を指す。

## ファイル構成

| ファイル | 役割 |
|---|---|
| `toshima-gomi.json` | **収集曜日データ**(区の収集曜日一覧PDF由来)。ページ起動時に fetch で読み込む |
| `app-data.json` | 公式ごみ分別アプリWeb版のスナップショット(分別辞典774品目・FAQ・連絡先・便利帳画像) |
| `crawl.py` | 公式Web版を再クロールして `app-data.json` を作り直す |
| `index.template.html` | ページ本体のテンプレート(CSS/JS 含む) |
| `build.py` | `index.template.html` + `app-data.json` → `index.html` を生成 |
| `index.html` | **生成物。直接編集しないこと**(build.py で上書きされる) |

## データ更新の手順

### 収集曜日データ(toshima-gomi.json)を更新する場合

1. マスターは `../Shared/toshima-gomi.json`(iOS版と共有)。そちらを更新してから、ここへコピーする

   ```sh
   cp ../Shared/toshima-gomi.json toshima-gomi.json
   ```

2. コミットして公開するだけ。**build.py の再実行は不要**(実行時に fetch で読むため)

### 分別辞典・連絡先など(app-data.json)を公式の最新に更新する場合

```sh
python3 crawl.py --dry-run   # 公式Web版を取得し、前回からの変更点を表示(書き込まない)
python3 crawl.py             # 問題なければ app-data.json を上書き
python3 build.py             # index.html を再生成
```

を実行し、続けて iOS版にも反映してから(`../README.md` の「公式データの更新手順」)、
コミットして公開する。

- 分別辞典の読み仮名 `yomi` は公式に無い自前データ。既存の値を品目名で引き継ぐので、
  公式に品目が増えたときは警告に出た品目の `yomi` を `app-data.json` に手で書き足す
- 公式側の分別区分の表記ゆれは `crawl.py` の `CATEGORY_ALIASES` で正式名に揃える。
  未知の区分が現れたら止まるので、表記ゆれか新区分かを判断して対応する
- **「区からのお知らせ」は取得しない。** 台風・年末年始などの時事情報は
  スナップショットにすると古い告知が残り続けるため、載せない方針
- 便利帳の画像は、公式側で更新されたものだけ縮小し直す(このときだけ Pillow が必要)

### テンプレート(index.template.html)を変更した場合

```sh
python3 build.py   # index.html を再生成
```

を実行してから、コミットして公開する。

## ローカルでの動作確認

`index.html` をブラウザで直接開くと fetch が制限されてデータを読み込めない。
必ずローカルサーバ経由で開くこと:

```sh
python3 -m http.server 8000
# → http://localhost:8000 を開く
```

## 関連

- iOS版(SwiftUI + WidgetKit)は本体リポジトリの直下にある。データの出典・照合手順は `../README.md` を参照。
