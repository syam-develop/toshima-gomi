#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""公式ごみ分別アプリWeb版を再クロールして、app-data.json と toshima-gomi.json を
作り直す。このアプリのデータの出典は公式Web版だけで、すべてここから取る。

  python3 crawl.py            # 取得 → 差分表示 → 2つのJSONを上書き
  python3 crawl.py --dry-run  # 取得と差分表示だけ (書き込まない)

取得元: https://manage.delight-system.com/threeR/web/ (jichitaiId=toshimaku)

  app-data.json
    bunbetsu   → jiten    分別辞典
    faq        → faq      よくある質問
    contact    → contacts 粗大ごみ申込 / 家電等の処分 / 食べきり協力店 / お問い合わせ先
    benri      → benri    便利帳 (ポスター画像は benriImg へ POST して data URI 化)

  toshima-gomi.json
    selectArea → 地域の一覧 (町 → 丁目 → 番 → 号。末端の地域ごとに areaId が付く)
    calendar   → areas[] の収集曜日・zone (末端の地域ごとに収集日カレンダーを
                 1か月ぶん取り、「毎週◯曜」「第n◯曜」のルールに読み替える)

収集曜日について:
  エリアの区切り (id・町名・丁目・表示名・番地条件) は公式には無く、こちらで
  決めている。toshima-gomi.json の areas[].officialAreas に「このエリアは公式の
  どの地域にあたるか」が書いてあり、その対応を使って曜日と zone だけを入れ替える。
  公式側で地域が増減・改名されたら、対応が取れなくなった地域を挙げて止まるので、
  officialAreas (必要ならエリアの区切り) を直してから再実行する。

  公式の全地域 (約90) のカレンダーを1ページずつ取るため、4分ほどかかる
  (1ページ約1.7MB。サーバに負荷をかけないよう1リクエストごとに待つ)。

取得しないもの:
  info (区からのお知らせ) — 台風・年末年始などの時事情報で、スナップショットに
  すると古い告知が残り続けて誤解を招く。追従できない情報は載せない方針のため
  対象外 (最新情報は区公式サイトで確認するよう、アプリのフッターで案内している)。

このスクリプトが作れないもの:
  jiten[].yomi (品目名の読み仮名) は公式に無い自前データ。既存の app-data.json
  から品目名をキーに引き継ぐ。公式に新しい品目が増えたら yomi は空で出力して
  警告するので、app-data.json に手で読みを書き足すこと。

辞典・FAQ・連絡先・便利帳は地域ID (areaId) を付けずに取得する。区内共通で、
地域を指定してもしなくても内容は変わらない。

標準ライブラリのみで動く (便利帳の画像が公式で更新されたときだけ、縮小の
ために Pillow が要る)。サーバに負荷をかけないよう1リクエストごとに待つ。
"""
import argparse
import base64
import calendar
import datetime
import hashlib
import html as htmllib
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DEFAULT = os.path.join(HERE, "app-data.json")
SCHEDULE_DEFAULT = os.path.join(HERE, "toshima-gomi.json")

BASE = "https://manage.delight-system.com/threeR/web"
JICHITAI = "toshimaku"
WAIT_SEC = 1.0
RETRIES = 3          # 通信エラー時の試行回数 (全体で140リクエストほどあるため)
RETRY_WAIT_SEC = 5.0

# 便利帳ポスター画像の縮小設定 (shrink_image 参照)
BENRI_WIDTH = 520
BENRI_COLORS = 128

# 公式サイドバーの contactN → app-data.json のキー
CONTACT_KEYS = {"contact0": "sodai", "contact1": "kaden",
                "contact2": "tabekiri", "contact3": "contact"}

# アプリ側 (Web版 JITEN_CAT_CLASS / iOS版 GomiCategory.forJitenCategory) が
# 色分けと絞り込みに使う分別区分。両アプリとも文字列の完全一致で判定している。
KNOWN_CATEGORIES = {
    "燃やすごみ",
    "資源（びん・かん・ペットボトル）",
    "資源（段ボール・紙・布類）",
    "資源（プラスチック）",
    "金属・陶器・ガラスごみ",
    "粗大ごみ",
    "区施設等拠点回収",
    "収集できません",
}

# 公式データ側の表記ゆれ → 正式な区分名。
# 2026-11 時点で「電池（ボタン電池）」だけ「等」の抜けた「区施設拠点回収」に
# なっている。そのまま載せるとアプリでは未知の区分になり、「収集できません」と
# 同じ色で表示されて「拠点回収」の絞り込みからも漏れる。
CATEGORY_ALIASES = {
    "区施設拠点回収": "区施設等拠点回収",
}


class CrawlError(Exception):
    pass


# ─────────────────────────────────────────────────────────────────
# 取得
# ─────────────────────────────────────────────────────────────────

_last_request = [0.0]
_cache_dir = [None]  # --cache で指定されたディレクトリ


def fetch(path, params, post=False):
    """BASE/path を取得して bytes を返す。post=True なら params をフォーム送信。

    --cache 指定時は応答をファイルに保存し、2回目以降はそれを使う
    (パーサを調整するときにサーバへ繰り返しアクセスしないため)。
    """
    cache_file = None
    if _cache_dir[0]:
        query = urllib.parse.urlencode(params)
        name = re.sub(r"[^\w.=-]", "_", f"{path}_{query}")
        if len(name) > 150:  # 地域名 (日本語) を含む問い合わせはファイル名の上限を超えうる
            name = f"{path}_{hashlib.sha1(query.encode()).hexdigest()}"
        cache_file = os.path.join(_cache_dir[0], name)
        if os.path.exists(cache_file):
            return open(cache_file, "rb").read()
    body = fetch_live(path, params, post)
    if cache_file:
        os.makedirs(_cache_dir[0], exist_ok=True)
        open(cache_file, "wb").write(body)
    return body


def fetch_live(path, params, post):
    query = urllib.parse.urlencode(params)
    url = f"{BASE}/{path}"
    req = urllib.request.Request(
        url if post else f"{url}?{query}",
        data=query.encode() if post else None,
        headers={"User-Agent": "Mozilla/5.0 (compatible; snapshot-crawler)"},
    )
    for attempt in range(1, RETRIES + 1):
        wait = WAIT_SEC - (time.monotonic() - _last_request[0])
        if wait > 0:
            time.sleep(wait)
        try:
            with urllib.request.urlopen(req, timeout=60) as res:
                return res.read()
        except OSError:  # 一時的な通信エラーなら待って取り直す。最後の1回はそのまま上げる
            if attempt == RETRIES:
                raise
            time.sleep(RETRY_WAIT_SEC)
        finally:
            _last_request[0] = time.monotonic()


def fetch_html(path, **params):
    params = {"jichitaiId": JICHITAI, "lang": "ja", **params}
    return fetch(path, params).decode("utf-8")


# ─────────────────────────────────────────────────────────────────
# 正規化
#
# iOS版への変換 (../scripts/check-ios-sync.py) は、ここで正規化済みの値を
# 再加工せずに使う。規則を変えると既存データ全件が差分になるので、変えるときは
# 両方直す。
# ─────────────────────────────────────────────────────────────────

def text_of(fragment):
    """HTML断片 → プレーンテキスト。タグは区切りなしで除去、前後の空白も除去。

    文中の空白 (全角スペース含む) は公式のまま残す。
    """
    return htmllib.unescape(re.sub(r"<[^>]+>", "", fragment)).strip()


# HTMLのまま載せてよいタグ (html_of 参照)。改行と、電話・Webへのリンクだけ。
SAFE_TAG_RE = re.compile(
    r'<br\s*/?>'
    r'|</a>'
    r'|<a href="(?:https?://|tel:)[^"<>\s]*"(?: target="_blank")?>')


def html_of(fragment):
    """HTML断片をHTMLのまま保持する (<br/> や <a> を残す)。前後の空白だけ除去。

    ここを通った値 (FAQの回答・連絡先の各欄) は、ページ側で innerHTML に
    そのまま流し込まれる。つまり公式サイトの内容が、こちらの公開ページ上で
    HTMLとして実行される。公式側が改ざんされたり想定外のタグを使い始めたり
    しても持ち込まないよう、SAFE_TAG_RE に無いタグ (script・img・イベント属性・
    javascript: のリンク等) が1つでもあれば中止する。書き換えて通すのではなく
    止めるのは、何が来たのかを人が見て判断するため。
    """
    html = fragment.strip()
    rest = SAFE_TAG_RE.sub("", html)
    if "<" in rest:
        at = rest.index("<")
        raise CrawlError(
            "許可していないHTMLが含まれています: "
            f"{rest[max(0, at - 20):at + 60]!r}。安全なタグなら SAFE_TAG_RE に"
            "追加し、そうでなければ公式サイトの内容を確認してください")
    return html


def jiten_text(fragment):
    """分別辞典の品目名・区分・注意事項用。text_of に加えて、空白文字
    (全角スペース等) を1文字ずつ半角スペースに置換する。

    連続する空白は潰さない (公式の桁揃え用スペースもそのまま残る)。
    辞典は検索対象なので、空白の種類だけ揃えておく。
    """
    return re.sub(r"\s", " ", text_of(fragment))


def jiten_keyword(keyword, name, cat, note_html):
    """data-keyword → 検索用文字列 kw。

    data-keyword は「品目名・区分・注意事項」と、その全文をカタカナ化した
    文字列を連結したもの。公式サーバは応答によって各部の間に空白を
    入れたり入れなかったりする (同じ日に両方の形が返ってくる) ので、
    空白に頼らず自前で4つに切り分け、半角スペース1個で連結し直す。
    こうしておけば、どちらの形の応答からも同じ kw ができる。
    """
    keyword = htmllib.unescape(keyword)
    # data-keyword 内の注意事項は <br/> が改行になっている
    note = text_of(re.sub(r"<br\s*/?>", "\n", note_html))
    head = "".join((name + cat + note).split())
    pos = matched = 0
    while matched < len(head) and pos < len(keyword):
        ch = keyword[pos]
        if not ch.isspace():
            if ch != head[matched]:
                break
            matched += 1
        pos += 1
    if matched != len(head):
        raise CrawlError(f"分別辞典「{name}」: data-keyword が "
                         "「品目名+区分+注意事項」で始まっていません")
    return " ".join(" ".join([name, cat, note, keyword[pos:]]).split())


def canonical_category(raw, item_name):
    """公式の分別区分文字列 → アプリが知っている区分名。

    表記ゆれは CATEGORY_ALIASES で吸収し、それでも未知の区分なら止める。
    黙って通すと、その品目はアプリで「収集できません」の色になってしまう
    (新しい区分が増えたのなら、アプリ側の色分け・絞り込みの対応が先に要る)。
    """
    cat = CATEGORY_ALIASES.get(raw, raw)
    if cat not in KNOWN_CATEGORIES:
        raise CrawlError(
            f"未知の分別区分 {raw!r} (品目: {item_name})。表記ゆれなら "
            "CATEGORY_ALIASES に、新しい区分なら KNOWN_CATEGORIES と両アプリの "
            "色分け・絞り込みに追加してから再実行してください")
    return cat


# ─────────────────────────────────────────────────────────────────
# パース
# ─────────────────────────────────────────────────────────────────

JITEN_RE = re.compile(
    r'<div class="panel panel-default" name="bunsetsuInfo"[^>]*'
    r'data-kana="([^"]*)" data-keyword="([^"]*)">\s*'
    r'<div class="panel-heading">\s*'
    r'<a class="panel-title" id="accordion_link">(.*?)</a>\s*'
    r'<div id="panel-item\d+" class="panel-item" hidden>\s*'
    r'<div class="detail-header">.*?</div>\s*'
    r'<div class="panel-body red" >(.*?)</div>\s*'
    r'<div class="detail-header">.*?</div>\s*'
    r'<div class="panel-body" id="panel-body\d+">(.*?)</div>', re.S)


def parse_jiten(page, yomi_by_name):
    """分別辞典。戻り値: (品目のリスト, 読み仮名の無い新規品目名,
    区分の表記ゆれを補正した品目 [(品目名, 公式の区分, 補正後の区分)])"""
    expected = len(re.findall(r'name="bunsetsuInfo"', page))
    items, missing_yomi, aliased = [], [], []
    for kana, keyword, name, cat, note in JITEN_RE.findall(page):
        kw = jiten_keyword(keyword, text_of(name), text_of(cat), note)
        name = jiten_text(name)
        raw_cat = jiten_text(cat)
        cat = canonical_category(raw_cat, name)
        if cat != raw_cat:
            aliased.append((name, raw_cat, cat))
            kw = kw.replace(raw_cat, cat)
        if name not in yomi_by_name:
            missing_yomi.append(name)
        items.append({"kana": kana, "name": name, "cat": cat,
                      "note": jiten_text(note), "kw": kw,
                      "yomi": yomi_by_name.get(name, "")})
    if not items or len(items) != expected:
        raise CrawlError(f"分別辞典: {expected}品目中 {len(items)}品目しか"
                         "パースできません (ページ構造が変わった可能性)")
    names = [it["name"] for it in items]
    if len(set(names)) != len(names):
        raise CrawlError("分別辞典: 品目名が重複しています")
    return items, missing_yomi, aliased


FAQ_RE = re.compile(
    r'<div class="index-header">(.*?)</div>'
    r'|<a id="accordion_link">(.*?)</a>\s*</h4>\s*</div>\s*'
    r'<div id="panel-item\d+" class="panel-item" hidden>\s*'
    r'<div class="panel-body" id="panel-body\d+" >(.*?)</div>', re.S)


def parse_faq(page):
    expected = len(re.findall(r'id="panel-item\d+"', page))
    faq, cat = [], ""
    for header, q, a in FAQ_RE.findall(page):
        if header:
            cat = text_of(header)
        else:
            faq.append({"cat": cat, "q": text_of(q), "a": html_of(a)})
    if not faq or len(faq) != expected:
        raise CrawlError(f"FAQ: {expected}件中 {len(faq)}件しかパースできません")
    return faq


ENTRY_START_RE = re.compile(
    r'<div class="panel panel-default">\s*<div class="panel-heading">\s*'
    r'<h4 class="panel-title index-header">')
FIELD_RE = re.compile(
    r'<div class="detail-header">(.*?)</div>\s*'
    r'<div class="detail-contents">(.*?)</div>', re.S)


def parse_contact_entries(page):
    """連絡先1ページ分のエントリ。エントリの開始位置でページを切り分け、
    各断片から 見出し(title) / 団体名(org) / 欄(電話番号・住所・備考…) を拾う。
    """
    starts = [m.start() for m in ENTRY_START_RE.finditer(page)]
    if len(starts) != len(re.findall(r'id="panel-item\d+"', page)):
        raise CrawlError("連絡先: エントリ数が合いません (ページ構造が変わった可能性)")
    entries = []
    for begin, end in zip(starts, starts[1:] + [len(page)]):
        chunk = page[begin:end]
        title = re.search(r'<a id="accordion_link"[^>]*>(.*?)</a>', chunk, re.S)
        org = re.search(r'<a class="panel-title"[^>]*>(.*?)</a>', chunk, re.S)
        fields = [{"label": text_of(label), "html": html_of(value)}
                  for label, value in FIELD_RE.findall(chunk)]
        if len(fields) != chunk.count('<div class="detail-header">'):
            raise CrawlError("連絡先: 欄を取りこぼしています (ページ構造が変わった可能性)")
        entries.append({"title": text_of(title.group(1)) if title else "",
                        "org": text_of(org.group(1)) if org else "",
                        "fields": fields})
    return entries


def page_title(page):
    m = re.search(r'<header id="header">\s*<div>(.*?)(?:<|</div>)', page, re.S)
    if not m:
        raise CrawlError("ページの見出しが見つかりません")
    return text_of(m.group(1))


def crawl_contacts(log, any_page):
    """サイドバー (どのページにも付いている。any_page から読む) で contact0〜3 の
    IDを拾い、サブメニューがあれば辿る。
    """
    # Cookie を持たない初回アクセスでは URL に ;jsessionid=… が挟まる
    menus = dict(re.findall(
        r'/threeR/web/contact(?:;jsessionid=\w+)?\?menu=(contact\d)&amp;id=(\d+)',
        any_page))
    if set(menus) != set(CONTACT_KEYS):
        raise CrawlError(f"連絡先メニューが想定と違います: {sorted(menus)}")
    contacts = {}
    for menu, key in CONTACT_KEYS.items():
        page = fetch_html("contact", menu=menu, id=menus[menu])
        sections = []
        direct = parse_contact_entries(page)
        if direct:
            sections.append({"label": "", "entries": direct})
        subs = re.findall(
            r'<a href="/threeR/web/contact[^"]*?[?&]id=(\d+)[^"]*" id="a_contact"[^>]*>\s*'
            r'<span id="contact">(.*?)</span>', htmllib.unescape(page), re.S)
        for sub_id, label in subs:
            sub = fetch_html("contact", menu=menu, id=sub_id)
            sections.append({"label": text_of(label),
                             "entries": parse_contact_entries(sub)})
        if not sections:
            raise CrawlError(f"連絡先 {key}: 内容が空です")
        contacts[key] = {"title": page_title(page), "sections": sections}
        log(f"  {key}: {sum(len(s['entries']) for s in sections)}件")
    return contacts


BENRI_RE = re.compile(
    r'<a class="panel-title" id="accordion_link">(.*?)</a>\s*'
    r'<div id="panel-item\d+" class="panel-item" hidden>(.*?)</div>', re.S)


def shrink_image(raw):
    """公式のポスター画像 (幅600〜1200px のフルカラーPNG、8枚で約4MB) を
    幅 BENRI_WIDTH・BENRI_COLORS 色のパレットPNGに縮小して data URI にする。

    app-data.json は index.html に丸ごと埋め込まれ、ページを開くたびに全部
    ダウンロードされるので、原寸のままは載せない。
    """
    try:
        from PIL import Image
    except ImportError:
        raise CrawlError("便利帳の画像が更新されています。縮小に Pillow が必要です: "
                         "python3 -m pip install Pillow")
    import io
    img = Image.open(io.BytesIO(raw)).convert("RGB")
    height = round(img.height * BENRI_WIDTH / img.width)
    img = img.resize((BENRI_WIDTH, height), Image.LANCZOS).quantize(BENRI_COLORS)
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def crawl_benri(log, old_benri, old_fetched_at):
    """便利帳。画像は公式の最終更新日 (edit_time) が前回取得日より前なら
    既存の縮小済み画像をそのまま使い、更新されたものだけ縮小し直す
    (縮小・減色は実行環境で結果が揺れるので、不要な再生成で差分を作らない)。
    """
    page = fetch_html("benri", menu="benri")
    old_img = {b["title"]: b["img"] for b in old_benri}
    benri = []
    for title, body in BENRI_RE.findall(page):
        title = text_of(title)
        image_id = re.search(r'<img id="imgId\d+" name="img" val="(\d+)"', body)
        if not image_id:
            raise CrawlError(f"便利帳「{title}」: 画像IDが見つかりません")
        res = json.loads(fetch("benriImg", {"jichitaiId": JICHITAI,
                                            "imageId": image_id.group(1)}, post=True))
        if not res.get("image_data") or not res.get("edit_time"):
            raise CrawlError(f"便利帳「{title}」: 画像を取得できません")
        edited = datetime.date.fromtimestamp(res["edit_time"] / 1000).isoformat()
        if title in old_img and old_fetched_at and edited < old_fetched_at:
            img, state = old_img[title], "変更なし"
        else:
            img = shrink_image(base64.b64decode(res["image_data"]))
            state = "画像を更新"
        # links はページ側で <a href> になる。javascript: 等を持ち込まない
        links = re.findall(r'<a href="([^"]+)"', body)
        for url in links:
            if urllib.parse.urlsplit(url).scheme not in ("http", "https"):
                raise CrawlError(f"便利帳「{title}」: http(s) 以外のリンクです: {url!r}")
        benri.append({"title": title, "links": links, "img": img})
        log(f"  {title} (公式の最終更新 {edited}: {state})")
    if not benri:
        raise CrawlError("便利帳: 1件もパースできません")
    return benri


# ─────────────────────────────────────────────────────────────────
# 収集曜日 (toshima-gomi.json)
# ─────────────────────────────────────────────────────────────────

# 曜日キー。datetime.date.weekday() の並び (月曜=0)
WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

# 公式カレンダーの種別名 → 読み取り用のキー。
# 紙とプラスチックは公式では別の種別だが、アプリは同じ日に集める前提で
# paperPlastic の1項目にまとめている (別の日になったら rules_from_calendar が止める)。
CALENDAR_KINDS = {
    "燃やすごみ": "burnable",
    "資源（びん・かん・ペットボトル）": "bottleCanPet",
    "資源（段ボール・紙・布類）": "paper",
    "資源（プラスチック）": "plastic",
    "金属・陶器・ガラスごみ": "metalCeramicGlass",
}

# 繁華街の地域に公式が付けている注記。これが付いた地域を zone=downtown にする。
# 締切時刻 (朝7:30) は toshima-gomi.json の meta.rules.deadlineDowntown と両アプリの
# コードに書いてあるので、文言が少しでも変わったら止めて人が確認する。
DOWNTOWN_NOTE = "池袋駅周辺繁華街地域です。収集曜日の朝７時３０分までに、指定の場所にお出しください。"

# crawl_schedule が公式の値で置き換える areas[] の項目 (これ以外は手で管理している)
SCHEDULE_FIELDS = ["zone", "bottleCanPet", "paperPlastic", "burnable", "metalCeramicGlass"]


def pick_sample_month(today):
    """収集曜日の読み取りに使う公式カレンダーの (年, 月) を決める (--month 省略時)。

    1か月ぶんの収集日から曜日のルールを逆算するので、その月が規則どおりに
    並んでいないと読み取れない (rules_from_calendar が中止する)。
    公式カレンダーで確かめたこと (2026-11 時点):
      - 12月は31日、1月は1〜3日の収集が載っていない (年末年始の休み)。
        その日が収集日にあたる地域で「毎週」「第1・3」が崩れるので読み取れない
      - 祝日には通常どおり収集が載っている (2026年11月・2027年4月で確認)
      - 過去の月 (2026年4月) も、来年度の月 (2027年4月) も同じルールで返ってくる

    TODO: いまは単純に「今月」を返している。12月・1月に実行すると読み取れずに
    中止するので、そのときどの月を使うかをここで決める。
    """
    return today.year, today.month


def select_area(*names):
    """公式の地域選択ダイアログと同じ問い合わせ。names で指定した階層
    (町 / 町・丁目 / 町・丁目・番) の1つ下の選択肢を返す。"""
    params = {"menu": "calendar", "jichitaiId": JICHITAI, "lang": "ja"}
    params.update({f"area{i}": name for i, name in enumerate(names, 1)})
    return json.loads(fetch("selectArea", params, post=True))


def crawl_official_areas(calendar_page):
    """公式の地域を末端まで辿る。戻り値: {path: {"areaId", "note"}}

    path は「池袋/2丁目/14番/5・8号」のように各階層の名前を / でつないだもの
    (町全体で1地域なら「池袋本町」)。areas[].officialAreas がこれを参照する。
    areaId は公式側で振り直されうるので保存せず、毎回ここで引き直す。
    """
    block = re.search(r'<select id="cmbArea1".*?</select>', calendar_page, re.S)
    towns = re.findall(r'<option value="\d+">([^<]+)</option>',
                       block.group(0)) if block else []
    if not towns:
        raise CrawlError("地域選択の町名一覧が見つかりません (ページ構造が変わった可能性)")
    found = {}

    def walk(names):
        # 応答の各行は「1つ下の階層の名前」と「その下にある末端の地域の数」を持つ。
        # 町を指定した応答なら area_name2 が丁目 (町全体で1地域なら空)。
        rows = select_area(*names)
        if not rows:
            raise CrawlError(f"地域 {'/'.join(names)}: 選択肢が返ってきません")
        for row in rows:
            name = (row.get(f"area_name{len(names) + 1}") or "").strip()
            path = names + (name,) if name else names
            where = "/".join(path)
            if row["area_id_count"] > 1:
                # まだ下の階層がある。問い合わせに使える階層は3つ (area1〜area3) まで
                if not name or len(path) > 3:
                    raise CrawlError(f"地域 {where}: 階層を辿れません (構造が変わった可能性)")
                before = len(found)
                walk(path)
                if len(found) - before != row["area_id_count"]:
                    raise CrawlError(f"地域 {where}: 末端の数が公式の件数と合いません")
            elif where in found:
                raise CrawlError(f"地域 {where}: 同じ名前の地域が2つあります")
            else:
                found[where] = {"areaId": row["area_id"],
                                "note": (row.get("note") or "").strip()}

    for town in towns:
        walk((text_of(town),))
    return found


def zone_of(path, note):
    if not note:
        return "normal"
    if note == DOWNTOWN_NOTE:
        return "downtown"
    raise CrawlError(
        f"地域 {path}: 注記が想定と違います: {note!r}\n"
        "繁華街の締切時刻などが変わっていないか確認し、DOWNTOWN_NOTE "
        "(必要ならアプリ側の締切時刻) を直してから再実行してください")


# カレンダーの表の1マス。月の前後の空きマスは class="td td" で、日付の番号が付かない
CALENDAR_CELL_RE = re.compile(r'<td class="td td(\d*)">(.*?)</td>', re.S)
KIND_NAME_RE = re.compile(r'<span class="trash_kind_name">(.*?)</span>', re.S)


def fetch_calendar(area_id, year, month):
    # month は必ず2桁で渡す。1桁だと公式サーバは別の年月として解釈する
    # (year=2027&month=4 で2030年4月の表が返る)。parse_calendar の説明も参照。
    return fetch_html("calendar", menu="calendar", areaId=area_id,
                      year=year, month=f"{month:02d}")


def parse_calendar(page, path, year, month):
    """収集日カレンダー1か月ぶん → {日: [種別名…]} (収集の無い日は空リスト)

    頼んだ地域・頼んだ月のページであることを確かめてから読む。公式サーバは
    指定を解釈できないとき、エラーにせず別の地域や別の月を返してくる。特に月は、
    見出しには頼んだ年月をそのまま出すのに、表の中身は別の月ということがある
    (fetch_calendar 参照)。見出しでは気づけないので、表の日付が本物の暦と同じ
    曜日の列に並んでいるかで確かめる。
    """
    shown = re.search(r'<span id="selectedArea">(.*?)</span>', page, re.S)
    ym = re.search(r'<span class="disp_date">\s*(\d{4})年(\d{1,2})月', page)
    begin = page.find('<table class="calendar')
    end = page.find('<div class="trash_kind_legend">')
    if not shown or not ym or not 0 <= begin < end:
        raise CrawlError(f"地域 {path}: 収集日カレンダーを読めません (ページ構造が変わった可能性)")
    if text_of(shown.group(1)) != path.replace("/", ""):
        raise CrawlError(f"地域 {path}: 別の地域 ({text_of(shown.group(1))}) のカレンダーが返ってきました")
    if (int(ym.group(1)), int(ym.group(2))) != (year, month):
        raise CrawlError(f"地域 {path}: {year}年{month}月を頼んだのに "
                         f"{ym.group(1)}年{ym.group(2)}月のカレンダーが返ってきました")

    last_day = calendar.monthrange(year, month)[1]
    days = {}
    for row in re.findall(r"<tr>(.*?)</tr>", page[begin:end], re.S):
        for column, (day, cell) in enumerate(CALENDAR_CELL_RE.findall(row)):
            if not day:
                continue
            day = int(day)
            # 表は日曜始まり (列0=日曜)。date.weekday() は月曜=0
            if (day in days or not 1 <= day <= last_day
                    or column != (datetime.date(year, month, day).weekday() + 1) % 7):
                raise CrawlError(
                    f"地域 {path}: 表の日付の並びが {year}年{month}月の暦と合いません "
                    f"({day}日が{'日月火水木金土'[column % 7]}曜の列)。"
                    "別の月の表が返ってきた可能性があります")
            days[day] = [text_of(kind) for kind in KIND_NAME_RE.findall(cell)]
    if len(days) != last_day:
        raise CrawlError(f"地域 {path}: カレンダーの日付が{len(days)}日ぶんしかありません "
                         "(ページ構造が変わった可能性)")
    return days


def rules_from_calendar(days, year, month, path):
    """1か月ぶんの収集日 → 曜日のルール (areas[] の収集曜日4項目)。

    公式カレンダーは「何日に何を集めるか」しか持たないので、ここで
      燃やすごみ / びん・かん・ペットボトル / 紙・プラスチック → 毎週◯曜
      金属・陶器・ガラスごみ                                 → 第1・3 か 第2・4 ◯曜
    に読み替える。「第n◯曜」は「その月に n 回目に来るその曜日」なので日付だけで
    決まる (1〜7日が1回目、8〜14日が2回目…。アプリ側の判定と同じ数え方)。

    この読み替えが成り立たない月は、間違ったルールを作らずに止める
    (どんな月がそうなるかは pick_sample_month を参照)。
    """
    weekday_of = lambda day: WEEKDAYS[datetime.date(year, month, day).weekday()]
    times_in_month = dict.fromkeys(WEEKDAYS, 0)          # 各曜日がその月に何回あるか
    seen = {key: {} for key in CALENDAR_KINDS.values()}  # キー → {曜日: [何回目…]}
    for day, kinds in sorted(days.items()):
        times_in_month[weekday_of(day)] += 1
        for kind in kinds:
            if kind not in CALENDAR_KINDS:
                raise CrawlError(
                    f"地域 {path}: 未知の収集種別 {kind!r}。新しい種別なら CALENDAR_KINDS と "
                    "両アプリの収集日の計算に追加してから再実行してください")
            nths = seen[CALENDAR_KINDS[kind]].setdefault(weekday_of(day), [])
            nths.append((day + 6) // 7)
            if len(set(nths)) != len(nths):
                raise CrawlError(f"地域 {path}: {month}月{day}日に {kind} が重複しています")

    hint = (f"{year}年{month}月のカレンダーからは曜日のルールを読み取れません。"
            "規則どおりの月を --month で指定してください")
    weekly = {}
    for key in ("burnable", "bottleCanPet", "paper", "plastic"):
        if not seen[key]:
            raise CrawlError(f"地域 {path}: {key} の収集日がありません。{hint}")
        for weekday, nths in seen[key].items():
            if len(nths) != times_in_month[weekday]:
                raise CrawlError(f"地域 {path}: {key} が {weekday} の毎週ではなく "
                                 f"第{nths} だけにあります。{hint}")
        weekly[key] = sorted(seen[key], key=WEEKDAYS.index)
    if weekly["paper"] != weekly["plastic"]:
        raise CrawlError(
            f"地域 {path}: 紙 ({weekly['paper']}) とプラスチック ({weekly['plastic']}) の"
            "曜日が違います。アプリは同じ日に集める前提なので、データの持ち方から見直しが必要です")
    metal = seen["metalCeramicGlass"]
    if len(metal) != 1 or next(iter(metal.values())) not in ([1, 3], [2, 4]):
        raise CrawlError(f"地域 {path}: 金属・陶器・ガラスごみが「第1・3」「第2・4」の"
                         f"どちらでもありません: {metal}。{hint}")
    (weekday, nth), = metal.items()
    return {"bottleCanPet": weekly["bottleCanPet"],
            "paperPlastic": weekly["paper"],
            "burnable": weekly["burnable"],
            "metalCeramicGlass": {"weekday": weekday, "nth": nth}}


def crawl_schedule(log, old_areas, year, month):
    """areas[] の収集曜日と zone (SCHEDULE_FIELDS) を公式の値で置き換えて返す。

    エリアの区切りは old_areas のまま。各エリアの officialAreas に挙げた公式の
    地域をすべて読み、その中で値が割れたら (= 区切りが公式と合わなくなったら) 止める。
    """
    official = crawl_official_areas(fetch_html("calendar", menu="calendar"))
    owner = {}
    for area in old_areas:
        if not area.get("officialAreas"):
            raise CrawlError(f"エリア {area['id']}: officialAreas がありません")
        for path in area["officialAreas"]:
            if path in owner:
                raise CrawlError(f"公式の地域 {path} が2つのエリア "
                                 f"({owner[path]}, {area['id']}) に入っています")
            owner[path] = area["id"]
    gone = sorted(set(owner) - set(official))
    added = sorted(set(official) - set(owner))
    if gone or added:
        raise CrawlError(
            "公式の地域と toshima-gomi.json の officialAreas が対応しません "
            "(公式側で地域が増減・改名された可能性)。\n"
            + "".join(f"  公式に見つからない: {p} (エリア {owner[p]})\n" for p in gone)
            + "".join(f"  どのエリアにも入っていない公式の地域: {p}\n" for p in added)
            + "officialAreas (必要ならエリアの区切り) を直してから再実行してください")
    log(f"  公式の{len(official)}地域を{len(old_areas)}エリアに対応付け。"
        f"{year}年{month}月のカレンダーから読み取ります")

    new_areas = []
    for count, area in enumerate(old_areas, 1):
        read = {}
        for path in area["officialAreas"]:
            page = fetch_calendar(official[path]["areaId"], year, month)
            days = parse_calendar(page, path, year, month)
            read[path] = {"zone": zone_of(path, official[path]["note"]),
                          **rules_from_calendar(days, year, month, path)}
        first = area["officialAreas"][0]
        for path, values in read.items():
            if values != read[first]:
                raise CrawlError(
                    f"エリア {area['id']} の中で公式の値が割れています:\n"
                    f"  {first}: {short(read[first], 200)}\n"
                    f"  {path}: {short(values, 200)}\n"
                    "エリアを分けるなど、toshima-gomi.json の区切りを直してください")
        new_areas.append({**area, **read[first]})
        if count % 10 == 0 or count == len(old_areas):
            log(f"  {count}/{len(old_areas)}エリア")
    return new_areas


def dump_schedule(data):
    """toshima-gomi.json の書式 (1項目1行・配列は1行) の文字列にする。

    json.dumps(indent=2) だと配列が1要素1行に開き、58エリアで行数が倍以上に
    なって、曜日が変わったときの差分が読みにくい。
    """
    one = lambda value: json.dumps(value, ensure_ascii=False)
    meta = json.dumps(data["meta"], ensure_ascii=False, indent=2).replace("\n", "\n  ")
    blocks = []
    for area in data["areas"]:
        rows = []
        for key, value in area.items():
            if key == "metalCeramicGlass":
                text = '{ "weekday": %s, "nth": %s }' % (one(value["weekday"]),
                                                          one(value["nth"]))
            else:
                text = one(value)
            rows.append(f"      {one(key)}: {text}")
        blocks.append("    {\n" + ",\n".join(rows) + "\n    }")
    return ('{\n  "meta": ' + meta + ',\n  "areas": [\n'
            + ",\n".join(blocks) + "\n  ]\n}\n")


# ─────────────────────────────────────────────────────────────────
# 差分表示
# ─────────────────────────────────────────────────────────────────

def short(v, limit=70):
    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
    return s if len(s) <= limit else s[:limit] + "…"


def diff_keyed(label, old, new, key, fields, out):
    old_by, new_by = {key(x): x for x in old}, {key(x): x for x in new}
    for k in old_by.keys() - new_by.keys():
        out.append(f"{label}[{short(k, 40)}]: 削除")
    for k in new_by.keys() - old_by.keys():
        out.append(f"{label}[{short(k, 40)}]: 追加")
    for k, n in new_by.items():
        o = old_by.get(k)
        if o is None:
            continue
        for f in fields:
            if o.get(f) != n.get(f):
                out.append(f"{label}[{short(k, 40)}].{f}:\n"
                           f"      旧: {short(o.get(f), 200)}\n"
                           f"      新: {short(n.get(f), 200)}")


def diff_snapshot(old, new):
    out = []
    diff_keyed("jiten", old.get("jiten", []), new["jiten"],
               lambda x: x["name"], ["kana", "cat", "note", "kw", "yomi"], out)
    diff_keyed("faq", old.get("faq", []), new["faq"],
               lambda x: x["q"], ["cat", "a"], out)
    for key, page in new["contacts"].items():
        old_page = old.get("contacts", {}).get(key, {"title": None, "sections": []})
        if old_page["title"] != page["title"]:
            out.append(f"contacts.{key}.title: {old_page['title']} → {page['title']}")
        flat = lambda p: [
            {"id": f"{s['label']}#{i} {e['title'] or e['org']}", **e}
            for s in p["sections"] for i, e in enumerate(s["entries"])]
        diff_keyed(f"contacts.{key}", flat(old_page), flat(page),
                   lambda x: x["id"], ["title", "org", "fields"], out)
    diff_keyed("benri", old.get("benri", []), new["benri"],
               lambda x: x["title"], ["links", "img"], out)
    return out


def diff_schedule(old_areas, new_areas):
    out = []
    diff_keyed("areas", old_areas, new_areas, lambda a: a["id"], SCHEDULE_FIELDS, out)
    return out


# ─────────────────────────────────────────────────────────────────

def month_arg(text):
    m = re.fullmatch(r"(\d{4})-(\d{1,2})", text)
    if not m or not 1 <= int(m.group(2)) <= 12:
        raise argparse.ArgumentTypeError("YYYY-MM の形で指定してください (例: 2026-11)")
    return int(m.group(1)), int(m.group(2))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", default=OUT_DEFAULT, help="出力先 (既定: app-data.json)")
    ap.add_argument("--schedule", default=SCHEDULE_DEFAULT,
                    help="収集曜日データ。エリアの区切りをここから読み、曜日を"
                         "入れ替えて書き戻す (既定: toshima-gomi.json)")
    ap.add_argument("--month", type=month_arg, metavar="YYYY-MM",
                    help="収集曜日を読み取る公式カレンダーの月 "
                         "(省略時は pick_sample_month が決める)")
    ap.add_argument("--dry-run", action="store_true", help="差分表示だけで書き込まない")
    ap.add_argument("--cache", metavar="DIR",
                    help="応答を DIR に保存して再利用する (パーサ調整用)")
    args = ap.parse_args()
    _cache_dir[0] = args.cache
    log = lambda s: print(s, flush=True)

    try:
        old = json.load(open(args.out, encoding="utf-8"))
    except OSError:
        old = {}
    yomi_by_name = {it["name"]: it["yomi"] for it in old.get("jiten", [])
                    if it.get("yomi")}
    try:
        old_schedule = json.load(open(args.schedule, encoding="utf-8"))
    except OSError as e:  # エリアの区切りは公式に無いので、これが無いと作れない
        print(f"中止: 収集曜日データ (エリアの区切り) を読めません: {e}", file=sys.stderr)
        return 1
    year, month = args.month or pick_sample_month(datetime.date.today())

    try:
        log("分別辞典を取得中…")
        jiten, missing_yomi, aliased = parse_jiten(
            fetch_html("bunbetsu", menu="bunbetsu"), yomi_by_name)
        log(f"  {len(jiten)}品目")
        log("FAQを取得中…")
        faq_page = fetch_html("faq", menu="faq")
        faq = parse_faq(faq_page)
        log(f"  {len(faq)}件")
        log("連絡先を取得中…")
        contacts = crawl_contacts(log, faq_page)
        log("便利帳を取得中…")
        benri = crawl_benri(log, old.get("benri", []), old.get("fetchedAt"))
        log("収集曜日を取得中… (公式の全地域のカレンダーを取るので4分ほどかかります)")
        areas = crawl_schedule(log, old_schedule["areas"], year, month)
    except CrawlError as e:
        print(f"\n中止: {e}", file=sys.stderr)
        return 1
    except OSError as e:  # 通信エラー (urllib の URLError / タイムアウト)
        print(f"\n中止: 公式Web版を取得できません: {e}", file=sys.stderr)
        return 1

    # 2つのファイルは同じ出典を同じときに取ったものなので、取得日を揃える
    # (アプリの出典表示は、この日付を1つだけ出す)
    today = datetime.date.today().isoformat()
    new = {"jiten": jiten, "faq": faq, "contacts": contacts, "benri": benri,
           "fetchedAt": today}
    new_schedule = {"meta": {**old_schedule["meta"], "fetchedAt": today},
                    "areas": areas}

    for name, raw, cat in aliased:
        log(f"\n表記ゆれを補正: {name} の区分「{raw}」→「{cat}」")
    diffs = diff_snapshot(old, new) + diff_schedule(old_schedule["areas"], areas)
    log(f"\n前回 ({old.get('fetchedAt', 'なし')}) からの変更: {len(diffs)}件")
    for d in diffs:
        log("  " + d)
    if missing_yomi:
        log(f"\n⚠ 読み仮名 (yomi) の無い新規品目が {len(missing_yomi)}件あります。"
            "app-data.json に手で書き足してください:")
        for name in missing_yomi:
            log("  " + name)

    if args.dry_run:
        log("\n--dry-run のため書き込みません")
        return 0
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(new, f, ensure_ascii=False)
    with open(args.schedule, "w", encoding="utf-8") as f:
        f.write(dump_schedule(new_schedule))
    log(f"\n{args.out} と {args.schedule} を更新しました。python3 build.py で "
        "index.html を再生成し、iOS版にも反映してください (手順は ../README.md)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
