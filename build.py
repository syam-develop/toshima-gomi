#!/usr/bin/env python3
"""index.html を生成する。

  python3 build.py

index.template.html のプレースホルダに以下を注入する:
  - app-data.json     : 公式ごみ分別アプリWeb版のスナップショット
                        (分別辞典774品目 / FAQ / 連絡先 / お知らせ / 便利帳画像)

収集曜日データ (toshima-gomi.json) は埋め込まず、ページ起動時に fetch で
読み込む。データ更新時は toshima-gomi.json を差し替えるだけでよく、
このスクリプトの再実行は template や app-data.json を変えたときだけ必要。

app-data.json を作り直したい場合は、公式Web版
https://manage.delight-system.com/threeR/web/init/toshimaku
を再クロールして同じ構造のJSONを生成すること(構造はキー参照)。
"""
import json
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    master = json.load(open(os.path.join(HERE, 'app-data.json'), encoding='utf-8'))
    html = open(os.path.join(HERE, 'index.template.html'), encoding='utf-8').read()

    for placeholder, obj in [
        ('const APP_DATA = /* __MASTER__ */ null;', master),
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


if __name__ == '__main__':
    main()
