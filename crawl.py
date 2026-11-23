#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""公式ごみ分別アプリWeb版を再クロールして app-data.json を作り直す。

  python3 crawl.py            # 取得 → 差分表示 → app-data.json を上書き
  python3 crawl.py --dry-run  # 取得と差分表示だけ (書き込まない)

取得元: https://manage.delight-system.com/threeR/web/ (jichitaiId=toshimaku)
  bunbetsu   → jiten    分別辞典
  faq        → faq      よくある質問
  contact    → contacts 粗大ごみ申込 / 家電等の処分 / 食べきり協力店 / お問い合わせ先
  benri      → benri    便利帳 (ポスター画像は benriImg へ POST して data URI 化)

取得しないもの:
  info (区からのお知らせ) — 台風・年末年始などの時事情報で、スナップショットに
  すると古い告知が残り続けて誤解を招く。追従できない情報は載せない方針のため
  対象外 (最新情報は区公式サイトで確認するよう、アプリのフッターで案内している)。

このスクリプトが作れないもの:
  jiten[].yomi (品目名の読み仮名) は公式に無い自前データ。既存の app-data.json
  から品目名をキーに引き継ぐ。公式に新しい品目が増えたら yomi は空で出力して
  警告するので、app-data.json に手で読みを書き足すこと。

地域ID (areaId) は付けずに取得する。辞典・FAQ・連絡先は区内共通で、
地域を指定してもしなくても内容は変わらない。

標準ライブラリのみで動く (便利帳の画像が公式で更新されたときだけ、縮小の
ために Pillow が要る)。サーバに負荷をかけないよう1リクエストごとに待つ。
"""
import argparse
import base64
import datetime
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

BASE = "https://manage.delight-system.com/threeR/web"
JICHITAI = "toshimaku"
WAIT_SEC = 1.0

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
        name = re.sub(r"[^\w.=-]", "_", f"{path}_{urllib.parse.urlencode(params)}")
        cache_file = os.path.join(_cache_dir[0], name)
        if os.path.exists(cache_file):
            return open(cache_file, "rb").read()
    body = fetch_live(path, params, post)
    if cache_file:
        os.makedirs(_cache_dir[0], exist_ok=True)
        open(cache_file, "wb").write(body)
    return body


def fetch_live(path, params, post):
    wait = WAIT_SEC - (time.monotonic() - _last_request[0])
    if wait > 0:
        time.sleep(wait)
    query = urllib.parse.urlencode(params)
    url = f"{BASE}/{path}"
    req = urllib.request.Request(
        url if post else f"{url}?{query}",
        data=query.encode() if post else None,
        headers={"User-Agent": "Mozilla/5.0 (compatible; snapshot-crawler)"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as res:
            body = res.read()
    finally:
        _last_request[0] = time.monotonic()
    return body


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


# ─────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", default=OUT_DEFAULT, help="出力先 (既定: app-data.json)")
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
    except CrawlError as e:
        print(f"\n中止: {e}", file=sys.stderr)
        return 1
    except OSError as e:  # 通信エラー (urllib の URLError / タイムアウト)
        print(f"\n中止: 公式Web版を取得できません: {e}", file=sys.stderr)
        return 1

    new = {"jiten": jiten, "faq": faq, "contacts": contacts, "benri": benri,
           "fetchedAt": datetime.date.today().isoformat()}

    for name, raw, cat in aliased:
        log(f"\n表記ゆれを補正: {name} の区分「{raw}」→「{cat}」")
    diffs = diff_snapshot(old, new)
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
    log(f"\n{args.out} を更新しました。python3 build.py で index.html を再生成してください")
    return 0


if __name__ == "__main__":
    sys.exit(main())
