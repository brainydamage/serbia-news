# Vestomat — Serbian news on one page

**https://vestomat.rs** · in Russian: **https://vestomat.rs/ru/**

A Serbian news aggregator in the spirit of [infomate.club](https://infomate.club). One page brings together outlets
critical of SNS and close to it, state, regional and foreign media, Serbian Reddit and what people in Serbia search
for on Google.

- full headlines; tapping a headline opens a plate with the lead and a link to the article;
- headlines and leads translated into Russian (Claude, at build time);
- a one-sentence AI description of the article, written from its full text when a reader opens the news
  (Claude Haiku; a reported description is rewritten by a stronger model);
- three views: by outlet, a single feed filtered by section, and themes — news from different outlets about the same
  story, handy for comparing how each one covers it;
- Latin / Cyrillic for Serbian texts; search ignores the alphabet and diacritics;
- dinar exchange rate, weather and air quality in Belgrade.

## How it works

- `sources.json` — sections and feeds. An outlet is added as `{"name", "site", "feed"}` in its section;
  `"lang"` — for non-Serbian feeds, `"exclude"` — URL parts to drop (showbiz, reality TV),
  `"exclude_category"` — feed categories (`<category>`) to drop when the category is not in the URL,
  `"more_feeds"` — more feeds of the same outlet, `"nocache": true` — the site serves its feed from a stale cache,
  `"ranked": true` — do not sort by date.
  World news, sport and lifestyle are left out: general feeds filter them with `exclude` and `exclude_category`.
  Sites without RSS are read through a Google News search by site.
- `build.py` — downloads the feeds (Python 3.10+ standard library), translates what is new and writes `site/`:
  the `/` and `/ru/` pages, `data.json`, `sitemap.xml` and the contents of `static/`.
  If a feed is down, its news from the previous build is used.
- `translate.py` — translation through the Anthropic API, cached in `cache/translations.json`;
  `glossary.json` — names that must be transliterated rather than translated by meaning.
- `essences.py`, `article_text.py` — fetch and extract the text of new articles for the AI description; the texts are
  stored privately in the worker, never published with the site.
- `stories.py`, `stories_live.py` — group news into themes (local embeddings with fastembed, decisions by Claude).
- `template.html` — the page itself: the data is embedded into it and rendered with JS.
- `worker/` — a Cloudflare Worker: reader reports (bad translation, bad description) to Telegram, AI descriptions
  on demand with a cache and a daily cost cap, build cost stats, feed watchdog.
- `.github/workflows/build.yml` — build and publish to GitHub Pages.

## Running locally

```bash
python3 build.py
python3 -m http.server 8765 -d site
```

Then open http://localhost:8765. After editing `template.html` there is no need to download the feeds again:
`python3 build.py --render-only` rebuilds the pages from the already downloaded `site/data.json`.

Translation needs the `anthropic` library and an API key:

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
export ANTHROPIC_API_KEY="sk-ant-..."
.venv/bin/python build.py
```

Without a key the build still works, just without translation. On GitHub the key is the repository secret
`ANTHROPIC_API_KEY`. Article texts and themes need `requirements-articles.txt` and `requirements-stories.txt`;
without them the build runs without those features.

## Credits

The idea and the concept come from [infomate.club](https://infomate.club), a project of [Vas3k.Club](https://vas3k.club)
(source code: [github.com/vas3k/infomate.club](https://github.com/vas3k/infomate.club), Apache 2.0).
The code here is written from scratch, but this site would not exist without them.

---

## По-русски

Агрегатор новостей о Сербии в духе [infomate.club](https://infomate.club): на одной странице — критичные к SNS и
близкие к нему, государственные, региональные и иностранные СМИ, сербский Reddit и то, что сербы ищут в Google.

- заголовки целиком; нажатие на заголовок раскрывает плашку с подводкой и ссылкой на статью;
- перевод заголовков и подводок на русский (Claude, при сборке);
- описание статьи одной фразой от ИИ — по полному тексту, когда новость раскрыли (Claude Haiku; описание,
  на которое пожаловались, переписывает модель посильнее);
- три вида: по изданиям, общая лента с фильтром по разделам и темы — новости разных изданий об одном сюжете,
  удобно сравнивать, кто как подаёт одну новость;
- латиница / кириллица для сербских текстов, поиск без разницы в алфавите и диакритике;
- курс динара, погода и воздух в Белграде.

Как устроено и как запустить — в английской части выше: файлы и команды те же.
