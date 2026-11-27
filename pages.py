"""検索から直接たどり着けるページ(町名別・品目別・よくある質問)を書き出す。

build.py から呼ばれる。単独では実行しない。

Web版の本体 (index.html) は1枚の HTML を JavaScript で描き分けているので、
画面ごとの URL が無く、検索エンジンからは「町名を選んでください」の1画面にしか
見えない。そこで、同じデータ (app-data.json / toshima-gomi.json) から
1品目1ページ・1町名1ページの静的な HTML を作り、検索の入口にする。

書き出すもの (すべて build.py の再実行で丸ごと作り直す生成物):
  area/index.html        町名の一覧
  area/<町名>.html       町名ごとの収集する曜日 (丁目・番地で分かれる地域をまとめて載せる)
  jiten/index.html       分別辞典の品目一覧 (検索つき)
  jiten/<品目>.html      品目ごとの分別区分・注意事項・出し方
  faq/index.html         よくある質問
  benri/<hash>.png       便利帳のポスター画像 (index.html と品目ページの両方から読む)
  sitemap.xml            Search Console に送るページ一覧
  site.css               index.template.html の CSS をそのまま書き出したもの

見た目は本体と同じ部品 (site.css) に、静的ページ用の指定 (pages.css) を足して作る。
"""
import base64
import hashlib
import html
import json
import os
import re
import shutil
from datetime import date, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))

# 公開URL。App Store のサポートURLにも使っているので変わらない (../README.md「Web版の公開」)
BASE_URL = "https://syam-develop.github.io/toshima-gomi/"
APP_STORE_URL = "https://apps.apple.com/jp/app/id6797180484"
SITE_NAME = "豊島区ごみ収集カレンダー"
TITLE_SUFFIX = f"｜{SITE_NAME}(非公式)"

# build.py が毎回消して作り直すディレクトリ。手で置いたファイルは消えるので、ここには何も置かないこと
GENERATED_DIRS = ["area", "jiten", "faq", "benri"]

WEEKDAY_KEYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]  # date.weekday() の順
WEEKDAY_JP = {"mon": "月", "tue": "火", "wed": "水", "thu": "木", "fri": "金", "sat": "土", "sun": "日"}

# 収集の品目。index.template.html の CATEGORY_ORDER / CAT_PHRASES と同じ並び・区切り
CATEGORY_ORDER = ["burnable", "bottleCanPet", "paperPlastic", "metalCeramicGlass"]
CAT_PHRASES = {
    "burnable": ["燃やすごみ"],
    "bottleCanPet": ["びん・かん・", "ペットボトル"],
    "paperPlastic": ["段ボール・", "紙・布類/", "プラスチック"],
    "metalCeramicGlass": ["金属・陶器・", "ガラスごみ"],
}

# 分別辞典の分別区分 → 色クラス (index.template.html の JITEN_CAT_CLASS と同じ)。
# 収集の品目と同じ色クラスのものは、そのまま収集する曜日の品目キーにもなる
JITEN_CAT_CLASS = {
    "燃やすごみ": "burnable",
    "資源（びん・かん・ペットボトル）": "bottleCanPet",
    "資源（段ボール・紙・布類）": "paperPlastic",
    "資源（プラスチック）": "paperPlastic",
    "金属・陶器・ガラスごみ": "metalCeramicGlass",
    "粗大ごみ": "cat-sodai",
    "区施設等拠点回収": "cat-kyoten",
    "収集できません": "cat-no",
}
# 品目ページの大きな帯に出すとき、改行してよい位置で区切ったもの
JITEN_CAT_PHRASES = {
    "燃やすごみ": ["燃やすごみ"],
    "資源（びん・かん・ペットボトル）": ["資源", "（びん・かん・", "ペットボトル）"],
    "資源（段ボール・紙・布類）": ["資源", "（段ボール・", "紙・布類）"],
    "資源（プラスチック）": ["資源", "（プラスチック）"],
    "金属・陶器・ガラスごみ": ["金属・陶器・", "ガラスごみ"],
    "粗大ごみ": ["粗大ごみ"],
    "区施設等拠点回収": ["区施設等", "拠点回収"],
    "収集できません": ["収集できません"],
}
# 分別区分ごとに添える便利帳のポスター (公式の題名)。公式側で題名が変わると止まる
BENRI_FOR_CAT = {
    "燃やすごみ": ["燃やすごみの分け方・出し方"],
    "資源（びん・かん・ペットボトル）": ["びん・かん・ペットボトルの分け方・出し方"],
    "資源（段ボール・紙・布類）": ["段ボール・紙・布類の分け方・出し方"],
    "資源（プラスチック）": ["プラスチックの分け方・出し方"],
    "金属・陶器・ガラスごみ": ["金属・陶器・ガラスごみの分け方・出し方"],
    "粗大ごみ": ["粗大ごみの出し方"],
    "区施設等拠点回収": ["小型家電・蛍光灯・乾電池（区施設等拠点回収）",
                       "廃食油・インクカートリッジ（区施設等拠点回収）"],
    "収集できません": [],
}
# 「収集できません」の品目のうち、注意事項に出てくる受付先が「家電・パソコン…の処分」
# (contacts.kaden) に載っているもの。(品目名か注意事項に含まれる語, kaden の見出し)
KADEN_SECTION_FOR = [
    ("家電リサイクル", "家電の購入先が不明もしくは買い替えない場合"),
    ("パソコン", "パソコンのメーカーが不明な場合"),
    ("注射針", "家庭で使用した注射針の回収薬局が不明な場合"),
]

# 粗大ごみ・拠点回収・収集できません の帯のアイコン (収集の4品目は index.template.html の ICONS を使う)
EXTRA_ICONS = {
    "cat-sodai":
        '<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M7 5h10a2 2 0 0 1 2 2v3.5a2.5 2.5 0 0 0-2 2.45V13H7v-.05A2.5 2.5 0 0 0 5 10.5V7a2 2 0 0 1 2-2z"/><path d="M2 13a2.5 2.5 0 0 1 5 0v1.5h10V13a2.5 2.5 0 0 1 5 0v5a1.5 1.5 0 0 1-1.5 1.5H20V21h-2v-1.5H6V21H4v-1.5h-.5A1.5 1.5 0 0 1 2 18z"/></svg>',
    "cat-kyoten":
        '<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M12 2.5 3 7.5V10h18V7.5zM5 11.5h2.5V18H5zm5.75 0h2.5V18h-2.5zm5.75 0H19V18h-2.5zM3 19.5h18V22H3z"/></svg>',
    "cat-no":
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" aria-hidden="true"><circle cx="12" cy="12" r="8.5"/><path d="M6 6l12 12"/></svg>',
}
NAV_ICONS = {
    "cal": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><rect x="3.5" y="5" width="17" height="15.5" rx="2.5"/><path d="M3.5 10h17M8 3v4M16 3v4"/></svg>',
    "faq": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><circle cx="12" cy="12" r="8.5"/><path d="M9.6 9.6a2.4 2.4 0 1 1 3.3 2.2c-.6.3-.9.8-.9 1.4v.6"/><path d="M12 16.9v.1" stroke-width="2.6"/></svg>',
}


def esc(s):
    return html.escape(str(s), quote=True)


def phrases(parts):
    """改行してよい位置で区切った語句を、折り返さない単位(.ph)で包んで並べる"""
    return "".join(f'<span class="ph">{esc(p)}</span>' for p in parts)


def js_object_strings(template, name):
    """index.template.html の `const NAME = { key: '<svg…>', … };` から {key: 文字列} を取り出す。
    アイコンの SVG を本体と二重に持たないため"""
    block = re.search(rf"const {name} = \{{(.*?)\n\}};", template, re.S)
    assert block, f"{name} が index.template.html に見つからない"
    pairs = dict(re.findall(r"(\w+):\s*'(<svg.*?</svg>)'", block.group(1), re.S))
    assert pairs, f"{name} からアイコンを読み取れない"
    return pairs


def item_slug(name):
    """品目ページのファイル名。品目名が同じなら毎回同じになる(URL が変わらない)。
    日本語のファイル名は macOS と Linux で文字の表し方 (NFD/NFC) が食い違うことがあるので使わない"""
    return hashlib.sha1(name.encode("utf-8")).hexdigest()[:8]


def town_slug(areas):
    """町名ページのファイル名。地域 ID の先頭 (例: dt-ikebukuro-2-a → ikebukuro) を使う"""
    slugs = {re.sub(r"^dt-", "", a["id"]).split("-")[0] for a in areas}
    assert len(slugs) == 1, f"町名の ID が揃っていない: {slugs}"
    return slugs.pop()


def fmt_jp_date(d, with_year=True):
    wd = WEEKDAY_JP[WEEKDAY_KEYS[d.weekday()]]
    return f"{d.year}年{d.month}月{d.day}日({wd})" if with_year else f"{d.month}月{d.day}日({wd})"


def closed_dates_text(closed):
    """収集の無い日を「2026年12月31日(木)〜2027年1月2日(土)」のように並べる"""
    days = sorted(date.fromisoformat(s) for s in closed)
    if not days:
        return ""
    runs, start, prev = [], days[0], days[0]
    for d in days[1:]:
        if d - prev != timedelta(days=1):
            runs.append((start, prev))
            start = d
        prev = d
    runs.append((start, prev))
    return "、".join(fmt_jp_date(a) if a == b else f"{fmt_jp_date(a)}〜{fmt_jp_date(b)}" for a, b in runs)


def chome_text(chomes):
    """[1,2,3,4] → 1〜4丁目、[1,2,5] → 1・2・5丁目"""
    cs = sorted(set(chomes))
    if len(cs) > 2 and cs == list(range(cs[0], cs[-1] + 1)):
        return f"{cs[0]}〜{cs[-1]}丁目"
    return "・".join(map(str, cs)) + "丁目"


def fmt_time(hhmm):
    return re.sub(r"^0(?=\d:)", "", hhmm)


def url_path(path):
    """jiten/index.html → jiten/ (index.html は付けない URL で載せる)"""
    return re.sub(r"(^|/)index\.html$", r"\1", path)


def href(root, path):
    """ページから見た相対リンク。トップ (path="") は root そのもの"""
    p = url_path(path)
    return root + p if root or p else "./"


class Site:
    def __init__(self, master, schedule, template, benri_paths):
        self.master = master
        self.schedule = schedule
        self.icons = js_object_strings(template, "ICONS")
        self.ui_icons = js_object_strings(template, "UI_ICONS")
        self.benri_paths = benri_paths  # 便利帳の題名 → 書き出した画像のパス
        self.fetched = schedule["meta"]["fetchedAt"]
        self.urls = []  # sitemap.xml に載せるページ (BASE_URL からの相対パス)

        # 町名 → 地域。並びは toshima-gomi.json の順 (本体の町名選択と同じ)
        self.towns = {}
        for a in schedule["areas"]:
            self.towns.setdefault(a["town"], []).append(a)
        self.town_slugs = {t: town_slug(areas) for t, areas in self.towns.items()}
        assert len(set(self.town_slugs.values())) == len(self.towns), "町名のファイル名が重複している"

        self.items = master["jiten"]
        self.item_slugs = {it["name"]: item_slug(it["name"]) for it in self.items}
        assert len(set(self.item_slugs.values())) == len(self.items), "品目のファイル名が重複している"
        for cat, titles in BENRI_FOR_CAT.items():
            for t in titles:
                assert t in benri_paths, f"便利帳に「{t}」が無い。BENRI_FOR_CAT を公式の題名に合わせる"
        unknown = {it["cat"] for it in self.items} - set(JITEN_CAT_CLASS)
        assert not unknown, f"分別区分が増えた。JITEN_CAT_CLASS などに足す: {unknown}"

    # ------------------------------------------------------------------
    # ページの枠
    # ------------------------------------------------------------------
    def write(self, path, *, title, description, crumbs, body, current, width="", scripts=""):
        """path: web/ からの相対パス (例: jiten/abcd1234.html)。crumbs: [(名前, path or None)]。
        width: 本文の幅 ("" = 980px / "wide" = 1280px / "page-narrow" = 760px)"""
        root = "../" * path.count("/")
        canonical = BASE_URL + url_path(path)
        self.urls.append(url_path(path))

        # 現在のページ (path が None) は item を省く (Google の推奨どおり)
        ld_items = []
        for i, (name, p) in enumerate(crumbs):
            entry = {"@type": "ListItem", "position": i + 1, "name": name}
            if p is not None:
                entry["item"] = BASE_URL + url_path(p)
            ld_items.append(entry)
        ld = json.dumps({"@context": "https://schema.org", "@type": "BreadcrumbList",
                         "itemListElement": ld_items}, ensure_ascii=False).replace("</", "<\\/")
        crumbs_html = "".join(
            f'<li><a href="{href(root, p)}">{esc(n)}</a></li>' if p is not None
            else f'<li aria-current="page">{esc(n)}</li>'
            for n, p in crumbs)

        nav = [
            ("cal", "カレンダー", "", NAV_ICONS["cal"]),
            ("area", "町名別", "area/", self.ui_icons["pin"]),
            ("jiten", "分別辞典", "jiten/", self.ui_icons["search"]),
            ("faq", "よくある質問", "faq/", NAV_ICONS["faq"]),
        ]

        def nav_links(tabbar):
            out = []
            for key, label, target, icon in nav:
                cur = ' class="on" aria-current="page"' if key == current else ""
                icon_html = f'<span class="ti">{icon}</span>' if tabbar else icon
                out.append(f'<a href="{href(root, target)}"{cur}>{icon_html}{esc(label)}</a>')
            return "".join(out)

        doc = f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>{esc(title)}</title>
<meta name="description" content="{esc(description)}">
<link rel="canonical" href="{esc(canonical)}">
<meta property="og:type" content="website">
<meta property="og:site_name" content="{esc(SITE_NAME)}">
<meta property="og:title" content="{esc(title)}">
<meta property="og:description" content="{esc(description)}">
<meta property="og:url" content="{esc(canonical)}">
<meta property="og:image" content="{BASE_URL}app-icon.png">
<meta name="theme-color" content="#0c7f72">
<meta name="apple-itunes-app" content="app-id=6797180484">
<link rel="icon" type="image/png" href="{root}app-icon.png">
<link rel="apple-touch-icon" href="{root}app-icon.png">
<link rel="stylesheet" href="{root}site.css">
<link rel="stylesheet" href="{root}pages.css">
<script>
// 本体で選んだ表示(自動・ライト・ダーク)をそのまま使う。描画前に決めてちらつきを防ぐ。
// iOS版は iPhone 専用なので、Android ではアプリの案内を出さない
(function () {{
  var root = document.documentElement, t = null;
  try {{ t = localStorage.getItem("toshimaGomi.theme"); }} catch (e) {{}}
  var dark = t === "dark" || (t !== "light" && matchMedia("(prefers-color-scheme: dark)").matches);
  root.dataset.theme = dark ? "dark" : "light";
  if (/Android/i.test(navigator.userAgent)) root.classList.add("android");
}})();
</script>
<script type="application/ld+json">{ld}</script>
</head>
<body>
<header class="topbar">
  <div class="topbar-inner">
    <a class="brand-link" href="{href(root, "")}">
      <img class="brand-icon" src="{root}app-icon.png" alt="" width="36" height="36">
      <span class="brand"><span class="brand-title">{esc(SITE_NAME)}</span></span>
    </a>
    <nav class="topnav" aria-label="メインメニュー">{nav_links(False)}</nav>
  </div>
</header>

<main class="wrap">
  <div class="narrow {width}">
    <nav class="crumbs" aria-label="現在の場所"><ol>{crumbs_html}</ol></nav>
{body}
  </div>
{self.footer(root)}
</main>

<nav id="tabbar" aria-label="メインメニュー"><div class="inner">{nav_links(True)}</div></nav>
{scripts}
</body>
</html>
"""
        out = os.path.join(HERE, path)
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, "w", encoding="utf-8") as f:
            f.write(doc)

    def footer(self, root):
        """本体の footerHtml() と同じ注意書き。文ごとに .ph で包む(間に空白を入れない)"""
        source = esc(self.schedule["meta"]["source"])
        return f"""<footer class="site-foot">
    <div class="promo">
      <a class="promo-link" href="{APP_STORE_URL}" target="_blank" rel="noopener">
        <span class="promo-title">アプリダウンロードはこちら</span>
        <img class="promo-icon" src="{root}app-icon.png" alt="" width="52" height="52">
      </a>
      <img class="promo-qr" src="{root}appstore-qr.svg" alt="App StoreのQRコード" width="92" height="92">
    </div>
    <p class="legal"><span class="ph">非公式アプリです。</span><span class="ph">最新情報は<a href="https://www.city.toshima.lg.jp/" target="_blank" rel="noopener">豊島区公式サイト</a>で確認してください。</span><span class="ph">祝日も通常どおり収集(年末年始のみ休止)。</span><span class="ph">荒天時は遅延・中止の場合があります。</span><br><span class="ph">出典: {source}</span><span class="ph">({esc(self.fetched)}取得)</span></p>
    <p class="legal foot-links"><a href="{root}area/">町名別の収集日</a><a href="{root}jiten/">分別辞典の品目一覧</a><a href="{root}faq/">よくある質問</a><a href="{root}privacy.html">プライバシーポリシー</a></p>
  </footer>"""

    # ------------------------------------------------------------------
    # 部品
    # ------------------------------------------------------------------
    def weekly_rows(self, area):
        """収集する曜日を1品目1段で並べる (本体の weeklyHtml() と同じ組み方)"""
        rows = []
        for k in CATEGORY_ORDER:
            if k == "metalCeramicGlass":
                spec = area[k]
                nth = "・".join(f"第{n}" for n in spec["nth"])
                day = f"<small>{nth}</small>{WEEKDAY_JP[spec['weekday']]}"
            else:
                days = [w for w in WEEKDAY_KEYS if w in area[k]]
                if not days:
                    continue
                day = "・".join(WEEKDAY_JP[w] for w in days)
            rows.append(f"""<div class="wk-row">
        <div class="wk-day">{day}</div>
        <div class="wk-cat {k}">{self.icons[k]}<span>{phrases(CAT_PHRASES[k])}</span></div>
      </div>""")
        return "".join(rows)

    def deadline(self, area):
        rules = self.schedule["meta"]["rules"]
        return fmt_time(rules["deadlineDowntown"] if area["zone"] == "downtown" else rules["deadlineNormal"])

    def town_grid(self, root, current=None):
        links = []
        for town, slug in self.town_slugs.items():
            cur = ' aria-current="page"' if town == current else ""
            links.append(f'<a href="{root}area/{slug}.html"{cur}>{esc(town)}</a>')
        return f'<div class="opt-grid link-grid">{"".join(links)}</div>'

    def benri_details(self, root, titles):
        out = []
        for b in self.master["benri"]:
            if b["title"] not in titles:
                continue
            links = ""
            if b["links"]:
                links = '<div class="benri-links">【備考】<br>' + "<br>".join(
                    f'<a href="{esc(u)}" target="_blank" rel="noopener">{esc(u)}</a>' for u in b["links"]) + "</div>"
            out.append(f"""<details class="acc">
        <summary>{esc(b["title"])}(区の資料)</summary>
        <div class="acc-body">
          <img class="benri-img" src="{root}{self.benri_paths[b["title"]]}" alt="{esc(b["title"])}" loading="lazy">
          {links}
        </div>
      </details>""")
        return "".join(out)

    def contact_cards(self, entries):
        """連絡先 (公式の HTML をそのまま使う。本体の contactEntriesHtml() と同じ)"""
        out = []
        for e in entries:
            fields = "".join(f'<div class="detail-label">{esc(f["label"])}</div><div class="rich">{f["html"]}</div>'
                             for f in e["fields"])
            title = f'<div class="contact-title">{esc(e["title"])}</div>' if e["title"] else ""
            org = f'<div class="contact-org">{esc(e["org"])}</div>' if e["org"] else ""
            out.append(f'<div class="card">{title}{org}{fields}</div>')
        return "".join(out)

    # ------------------------------------------------------------------
    # 町名ページ
    # ------------------------------------------------------------------
    def build_towns(self):
        root = "../"
        rules = self.schedule["meta"]["rules"]
        closed = closed_dates_text(self.schedule.get("closedDates", []))

        for town, areas in self.towns.items():
            slug = self.town_slugs[town]
            chomes = chome_text([c for a in areas for c in a["chome"]])
            plates = []
            for a in areas:
                zone = ('<span class="zone-tag">池袋駅周辺の繁華街エリア</span>'
                        if a["zone"] == "downtown" else "")
                plates.append(f"""<section class="plate area-plate" id="{esc(a["id"])}">
      <h2 class="plate-head">{esc(a["label"])}{zone}</h2>
      {self.weekly_rows(a)}
      <div class="plate-foot">
        <span class="deadline">{self.ui_icons["clock"]}<span>朝 {esc(self.deadline(a))} までに出す</span></span>
        <a class="pill-btn" href="{root}?area={esc(a["id"])}">この地域のカレンダーを開く</a>
      </div>
    </section>""")

            notes = [f'<li>収集日の朝 {fmt_time(rules["deadlineNormal"])} まで(池袋駅周辺の繁華街エリアは朝 {fmt_time(rules["deadlineDowntown"])} まで)に出してください。</li>',
                     "<li>金属・陶器・ガラスごみの「第1・3」は、その月の1回目・3回目のその曜日です(第◯週という意味ではありません)。</li>"]
            if closed:
                notes.append(f"<li>{esc(closed)}は収集がありません。</li>")

            body = f"""    <h1 class="page-title">{esc(town)}のごみ収集日<span class="page-sub">豊島区{esc(town)}{esc(chomes)}</span></h1>
    <ul class="notes">{"".join(notes)}</ul>
    <div class="area-grid">{"".join(plates)}</div>
    <h2 class="section-title">ほかの町名</h2>
    {self.town_grid(root, current=town)}"""
            self.write(
                f"area/{slug}.html",
                title=f"{town}のごみ収集日・曜日(豊島区{chomes}){TITLE_SUFFIX}",
                description=(f"豊島区{town}{chomes}のごみ収集日。燃やすごみ・びん・かん・ペットボトル・"
                             f"段ボール・紙・布類・プラスチック・金属・陶器・ガラスごみを出す曜日を、地域ごとに載せています。"),
                crumbs=[("トップ", ""), ("町名別の収集日", "area/index.html"), (town, None)],
                body=body, current="area")

        body = f"""    <h1 class="page-title">町名別のごみ収集日<span class="page-sub">町名を選ぶと、収集する曜日がわかります</span></h1>
    <section class="plate">
      <h2 class="plate-head">豊島区の町名</h2>
      {self.town_grid(root)}
    </section>"""
        self.write("area/index.html",
                   title=f"町名別のごみ収集日(豊島区){TITLE_SUFFIX}",
                   description="豊島区のごみ収集日を町名ごとにまとめています。燃やすごみ・資源・金属・陶器・ガラスごみを出す曜日を、丁目・番地ごとに確認できます。",
                   crumbs=[("トップ", ""), ("町名別の収集日", None)],
                   body=body, current="area")

    # ------------------------------------------------------------------
    # 品目ページ
    # ------------------------------------------------------------------
    def related_items(self, it, limit=12):
        """同じ分別区分の品目のうち、五十音で前後にあるもの"""
        same = [x for x in self.items if x["cat"] == it["cat"]]
        i = same.index(it)
        start = max(0, min(i - limit // 2, len(same) - limit - 1))
        return [x for x in same[start:start + limit + 1] if x is not it][:limit]

    def item_guide(self, it, root):
        """分別区分ごとの出し方。載せるのは公式データにある情報だけ"""
        cat = it["cat"]
        cls = JITEN_CAT_CLASS[cat]
        rules = self.schedule["meta"]["rules"]
        parts = []
        if cls in CATEGORY_ORDER:
            name = re.sub(r"^資源（(.*)）$", r"\1", cat)  # 資源（プラスチック） → プラスチック
            parts.append(f"""<section class="plate guide">
      <h2 class="plate-head">出す日</h2>
      <div class="guide-body">
        <p>{esc(name)}を出す曜日は、地域ごとに違います。町名を選ぶと、収集する曜日がわかります。</p>
        <p>収集日の朝 {fmt_time(rules["deadlineNormal"])} まで(池袋駅周辺の繁華街エリアは朝 {fmt_time(rules["deadlineDowntown"])} まで)に出してください。</p>
      </div>
      {self.town_grid(root)}
    </section>""")
        elif cat == "粗大ごみ":
            sodai = self.master["contacts"]["sodai"]
            entries = [e for s in sodai["sections"] for e in s["entries"]]
            parts.append(f"""<h2 class="section-title">{esc(sodai["title"])}</h2>
    {self.contact_cards(entries)}
    <div class="snapshot-note">インターネット申込は
      <a href="https://ecolife.e-tumo.jp/toshima-sodai-u/" target="_blank" rel="noopener">粗大ごみ受付センター(外部サイト)</a>
      から。</div>""")
        elif cat == "区施設等拠点回収":
            parts.append('<p class="guide-lead">回収している施設は、下の区の資料で確認できます。</p>')
        elif cat == "収集できません":
            kaden = self.master["contacts"]["kaden"]
            text = it["name"] + it["note"]
            labels = {label for word, label in KADEN_SECTION_FOR if word in text}
            for s in kaden["sections"]:
                if s["label"] in labels:
                    parts.append(f'<h2 class="section-title">{esc(s["label"])}</h2>{self.contact_cards(s["entries"])}')

        posters = self.benri_details(root, BENRI_FOR_CAT[cat])
        if posters:
            parts.append(f'<div class="acc-list">{posters}</div>')
        return "\n    ".join(parts)

    def build_items(self):
        root = "../"
        for it in self.items:
            cat = it["cat"]
            cls = JITEN_CAT_CLASS[cat]
            icon = self.icons.get(cls) or EXTRA_ICONS[cls]
            note = (f'<div class="answer-note"><div class="detail-label">注意事項</div>'
                    f'<p class="rich">{esc(it["note"])}</p></div>') if it["note"] else ""
            related = "".join(
                f'<a href="{self.item_slugs[x["name"]]}.html">{esc(x["name"])}</a>' for x in self.related_items(it))

            body = f"""    <section class="plate answer">
      <h1 class="plate-head">{esc(it["name"])}の捨て方</h1>
      <div class="answer-band {cls}">
        <span class="disc">{icon}</span>
        <span class="answer-cat">{phrases(JITEN_CAT_PHRASES[cat])}</span>
      </div>
      {note}
    </section>
    {self.item_guide(it, root)}
    <h2 class="section-title">ほかの「{esc(cat)}」の品目</h2>
    <div class="chip-links">{related}</div>
    <a class="pill-btn" href="./">分別辞典の品目一覧へ</a>"""
            desc = f"豊島区で「{it['name']}」を捨てるときの分別区分は「{cat}」です。"
            if it["note"]:
                desc += it["note"]
            if len(desc) > 120:
                desc = desc[:119] + "…"
            self.write(
                f"jiten/{self.item_slugs[it['name']]}.html",
                title=f"{it['name']}は「{cat}」{TITLE_SUFFIX}",
                description=desc,
                crumbs=[("トップ", ""), ("分別辞典", "jiten/index.html"), (it["name"], None)],
                body=body, current="jiten", width="page-narrow")

    def build_jiten_index(self):
        groups = {}
        for it in self.items:
            groups.setdefault(it["kana"], []).append(it)
        kana_nav = "".join(f'<a href="#k{i}">{esc(k)}</a>' for i, k in enumerate(groups))
        sections = []
        for i, (kana, items) in enumerate(groups.items()):
            links = "".join(
                f'<a class="jiten-link" href="{self.item_slugs[it["name"]]}.html" '
                f'data-k="{esc(it["name"] + " " + it.get("yomi", "") + " " + it["kw"])}">'
                f'<span class="jiten-name">{esc(it["name"])}</span>'
                f'<span class="jiten-badge {JITEN_CAT_CLASS[it["cat"]]}">{esc(it["cat"])}</span></a>'
                for it in items)
            sections.append(f'<section class="kana-group" id="k{i}"><h2 class="kana-header">{esc(kana)}</h2>'
                            f'<div class="jiten-list">{links}</div></section>')
        body = f"""    <h1 class="page-title">分別辞典<span class="page-sub">豊島区の資源・ごみの分け方 {len(self.items)}品目</span></h1>
    <label class="search-box">{self.ui_icons["search"]}
      <input type="search" id="jitenInput" placeholder="品目名で検索(例: でんち)" autocomplete="off" aria-label="品目名で検索">
    </label>
    <nav class="kana-nav" aria-label="五十音で移動">{kana_nav}</nav>
    <div class="jiten-count" id="jitenCount" aria-live="polite">{len(self.items)}件</div>
    {"".join(sections)}"""
        # 検索の正規化は本体の normSearch() と同じ (NFKC → 小文字 → ひらがなをカタカナに)
        script = """<script>
(function () {
  var input = document.getElementById("jitenInput");
  var count = document.getElementById("jitenCount");
  var links = Array.prototype.slice.call(document.querySelectorAll(".jiten-link"));
  var groups = Array.prototype.slice.call(document.querySelectorAll(".kana-group"));
  function norm(s) {
    return s.normalize("NFKC").toLowerCase().replace(/[ぁ-ん]/g, function (c) {
      return String.fromCharCode(c.charCodeAt(0) + 0x60);
    });
  }
  var keys = links.map(function (a) { return norm(a.dataset.k); });
  input.addEventListener("input", function () {
    var q = norm(input.value.trim()), n = 0;
    links.forEach(function (a, i) {
      var hit = !q || keys[i].indexOf(q) >= 0;
      a.hidden = !hit;
      if (hit) n++;
    });
    groups.forEach(function (g) { g.hidden = !g.querySelector(".jiten-link:not([hidden])"); });
    count.textContent = n ? n + "件" : "該当する品目がありません。別の言葉やひらがなで探してみてください";
  });
})();
</script>"""
        self.write("jiten/index.html",
                   title=f"ごみ分別辞典(豊島区・{len(self.items)}品目){TITLE_SUFFIX}",
                   description=f"豊島区の資源・ごみの分け方を{len(self.items)}品目まとめた分別辞典です。品目ごとに、燃やすごみ・資源・粗大ごみなどの分別区分と出し方の注意を確認できます。",
                   crumbs=[("トップ", ""), ("分別辞典", None)],
                   body=body, current="jiten", width="wide", scripts=script)

    # ------------------------------------------------------------------
    # よくある質問
    # ------------------------------------------------------------------
    def build_faq(self):
        html_parts, group, last = [], [], None

        def flush():
            if group:
                html_parts.append(f'<div class="acc-list">{"".join(group)}</div>')
                group.clear()

        for i, f in enumerate(self.master["faq"]):
            if f["cat"] != last:
                flush()
                last = f["cat"]
                html_parts.append(f'<h2 class="section-title">{esc(f["cat"])}</h2>')
            group.append(f"""<details class="acc" id="q{i + 1}">
        <summary><span class="jiten-name">{esc(f["q"])}</span></summary>
        <div class="acc-body rich">{f["a"]}</div>
      </details>""")
        flush()
        body = f"""    <h1 class="page-title">よくある質問<span class="page-sub">豊島区の資源・ごみの出し方</span></h1>
    {"".join(html_parts)}"""
        self.write("faq/index.html",
                   title=f"ごみ・資源のよくある質問(豊島区){TITLE_SUFFIX}",
                   description="豊島区のごみと資源の出し方について、よくある質問と答えをまとめています。出す時間、祝日の収集、粗大ごみの申し込み、有料ごみ処理券など。",
                   crumbs=[("トップ", ""), ("よくある質問", None)],
                   body=body, current="faq")

    # ------------------------------------------------------------------
    def write_sitemap(self):
        urls = [""] + self.urls  # "" = 本体 (index.html)
        lines = ['<?xml version="1.0" encoding="UTF-8"?>',
                 '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
        for u in urls:
            lines.append(f"  <url><loc>{esc(BASE_URL + u)}</loc><lastmod>{self.fetched}</lastmod></url>")
        lines.append("</urlset>")
        with open(os.path.join(HERE, "sitemap.xml"), "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        return len(urls)


def clean_generated():
    for d in GENERATED_DIRS:
        shutil.rmtree(os.path.join(HERE, d), ignore_errors=True)


def write_benri_images(master):
    """便利帳のポスター (data URI) を画像ファイルに書き出し、{題名: パス} を返す。
    ファイル名は中身のハッシュ。公式側で画像が差し替わると名前も変わり、古い画像のキャッシュを掴まない"""
    paths = {}
    os.makedirs(os.path.join(HERE, "benri"), exist_ok=True)
    for b in master["benri"]:
        head, data = b["img"].split(",", 1)
        assert head == "data:image/png;base64", f"便利帳の画像が PNG ではない: {head[:40]}"
        raw = base64.b64decode(data)
        path = f"benri/{hashlib.sha1(raw).hexdigest()[:10]}.png"
        with open(os.path.join(HERE, path), "wb") as f:
            f.write(raw)
        paths[b["title"]] = path
    return paths


def write_site_css(template):
    """本体の CSS (index.template.html の <style>) を静的ページからも読めるよう書き出す"""
    m = re.search(r"<style>\n(.*?)</style>", template, re.S)
    assert m, "index.template.html に <style> が無い"
    with open(os.path.join(HERE, "site.css"), "w", encoding="utf-8") as f:
        f.write("/* 生成物。直接編集しないこと (index.template.html の <style> を build.py が書き出したもの) */\n")
        f.write(m.group(1))


def build(master, schedule, template, benri_paths):
    write_site_css(template)
    site = Site(master, schedule, template, benri_paths)
    site.build_towns()
    site.build_items()
    site.build_jiten_index()
    site.build_faq()
    n = site.write_sitemap()
    print(f"built {len(site.towns)} town pages, {len(site.items)} item pages, sitemap.xml ({n} URLs)")
