#!/usr/bin/env python3
"""index.html と、検索の入口になる静的ページを生成する。

  python3 build.py

index.template.html のプレースホルダに以下を注入する:
  - app-data.json     : 公式ごみ分別アプリWeb版のスナップショット
                        (分別辞典774品目 / FAQ / 連絡先 / 便利帳画像)
便利帳のポスター画像は data URI のまま埋め込まず、benri/ に画像ファイルとして
書き出して、そのパスを埋め込む (index.html が約2MB軽くなる)。

続けて pages.py で、町名別・品目別・よくある質問の静的ページと sitemap.xml を
書き出す (area/ jiten/ faq/ benri/ は毎回消して作り直す)。

収集曜日データ (toshima-gomi.json) は index.html には埋め込まず、ページ起動時に
fetch で読み込む。ただし町名ページはこのデータから作るので、template・
app-data.json・toshima-gomi.json のどれかを変えたら、このスクリプトを再実行する
(crawl.py は2つの JSON を同時に更新するので、実行したら毎回)。

app-data.json と toshima-gomi.json を公式Web版
https://manage.delight-system.com/threeR/web/init/toshimaku
の最新内容で作り直すには crawl.py を使う。
"""
import copy
import json
import os
import re

import pages

HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    master = json.load(open(os.path.join(HERE, 'app-data.json'), encoding='utf-8'))
    schedule = json.load(open(os.path.join(HERE, 'toshima-gomi.json'), encoding='utf-8'))
    template = open(os.path.join(HERE, 'index.template.html'), encoding='utf-8').read()

    pages.clean_generated()
    benri_paths = pages.write_benri_images(master)
    assert len(benri_paths) == len(master['benri']), '便利帳の題名が重複している'
    embedded = copy.deepcopy(master)
    for b in embedded['benri']:
        b['img'] = benri_paths[b['title']]

    html = template
    for placeholder, obj in [
        ('const APP_DATA = /* __MASTER__ */ null;', embedded),
    ]:
        assert placeholder in html, f'placeholder not found: {placeholder}'
        name = placeholder.split(' ')[1]
        # JSON内に "</script>" 相当が現れてもタグが閉じないようエスケープ
        payload = json.dumps(obj, ensure_ascii=False,
                             separators=(',', ':')).replace('</', '<\\/')
        html = html.replace(placeholder, f'const {name} = {payload};')

    out = os.path.join(HERE, 'index.html')
    open(out, 'w', encoding='utf-8').write(html)
    print(f'built {out} ({os.path.getsize(out):,} bytes)')

    # 構文チェック用にscript部を抜き出せることだけ確認
    assert re.search(r'<script>\n.*?\n</script>', html, re.S)

    pages.build(master, schedule, template, benri_paths)


if __name__ == '__main__':
    main()
