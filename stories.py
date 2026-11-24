"""Сюжеты: подборки новостей разных изданий об одном событии. Векторы находят похожие сюжеты, модель решает.

На каждой сборке новые события (kind=event или без kind — русскоязычные ленты) раскладываются так:
1. вектор по «что произошло» (what), без него — по заголовку; fastembed, локально;
2. кандидаты — до CANDIDATES живых сюжетов (последняя новость не старше LIVE), сходство — с лучшей новостью сюжета,
   а не с его центром, чтобы сюжет не уплывал. Одиночка — тоже сюжет, просто из одной новости;
3. новости без кандидатов и без похожих соседей по сборке сразу становятся новыми сюжетами, остальные решает
   модель, пачками по BATCH: join (в сюжет S…), same (то же событие, что новая N…), new, unclear (тоже свой
   сюжет: склейка ещё может приложить новость к видимому);
4. раз в MERGE_EVERY — склейка (merge): похожие пары живых сюжетов модель проверяет на «одну историю» — событие
   или дело с развитием и реакциями; склеенные сюжеты остаются частями.
Видимый сюжет — от двух изданий. Названий пока нет. Раскладку проверяем прогоном на снимке архива против эталона
(stories_replay.py, stories_gold.py), в живой сборке она идёт в теневом режиме (stories_live.py).

Три уровня: сюжет → часть → новость. Часть — сюжет, каким он был до склейки: у новости story — сюжет,
part — id сюжета, в котором она появилась; склейка сливает сюжеты, а части сохраняет, чтобы тему потом можно было
показать по событиям и разделить обратно. Новость, добавленная в сюжет (join), идёт в часть самой похожей новости.

Состояние — один словарь, JSON: stories (id → новости и время), items (адрес → решение и то, что нужно для запроса).
Векторы хранятся отдельно: {адрес: вектор}.
"""
import json
from datetime import datetime, timedelta

import numpy as np

import translate

# поднять при изменении логики раскладки или склейки (правила, промпты и пороги отслеживаются и так):
# живая страница (stories_live.py) разложит все свои новости заново
# 2 — при раскладке модель видит и новость сюжета, самую похожую на новую; 3 — участники сравниваются латиницей;
# 4 — «непонятная» новость — свой сюжет, а не без сюжета; 5 — отдельные события выделяются в свои сюжеты;
# 6 — у видимого сюжета карточка события (cards), раскладка и склейка решают по ней;
# 7 — с 18.11 сюжет — тема (RULES): одна текущая ситуация со всеми эпизодами, а не одно событие. Деления нет
# (LINES_ON), склейку и перепроверку решает Haiku, карточка — ситуация темы и обновляется вместе с именем
LAYOUT_VERSION = 7
MODEL = "claude-haiku-5-5"
EFFORT = "low"
# e5-large против MiniLM на эталоне 09–10.11: нужная новость первой среди похожих в 96–97% случаев против 91–93%;
# на трудных парах (кандидатах для модели) при потере 5% одинаковых отсеивает 75–79% разных против 29–33%.
# Не хуже reranker bge-reranker-v2-m3 и в разы быстрее. e5 ждёт приставку «query: » перед текстом
EMBED_MODEL = "intfloat/multilingual-e5-large"
EMBED_PREFIX = "query: "
LIVE = timedelta(hours=48)
CANDIDATES = 8
# пороги — под e5 (у неё сходства сжаты к 0.8–0.95). На эталоне 09–10.11 у 0.855 полнота пар 0.80 при вдвое
# меньшем числе разных пар выше порога, чем у MiniLM с прежним порогом 0.5 (полнота там была 0.72)
CANDIDATE_SIM = 0.855  # ниже — даже не спрашиваем модель
# копия — почти дословный пересказ уже разложенной новости другим изданием: идёт туда же, где сейчас оригинал, без
# модели. Так одно решение не повторяется десятью: 15.11 первая новость МВД ко Дню памяти жертв ДТП легла в сюжет
# о сводке 13.11, а за ней ещё 13 копий со сходством до 1.0. И исправление (перепроверка, команда) уводит
# за оригиналом и будущие копии. На 15.11 выше 0.98 — 178 новостей из 1283, и каждая лежит там же, где оригинал;
# разные выступления Вучича доходят до 0.969
COPY_SIM = 0.98
STORY_ITEMS = 30  # с каким числом последних новостей сюжета сравниваем
SHOWN_ITEMS = 3  # сколько новостей сюжета видит модель: первая и последние
BATCH = 25
SKIP_SECTIONS = {"reddit", "trends"}

# что считать одной темой — общее для раскладки, склейки, перепроверки и проверки топа.
# До 18.11 единицей было одно событие («сюжет — не тема»), и одна ситуация распадалась на почти одинаковые сюжеты:
# конститутивное заседание Скупщины и выбор Чупича председателем лежали отдельно, а новости делились между ними
# наугад (одна новость Tanjug — в одном, B92 — в другом). Отличать событие от эпизода той же ситуации стоило
# больше всего: склейка Sonnet, перепроверка, деление больших сюжетов — около $2 из $2.9 в день
RULES = """Тема — одна текущая ситуация, о которой пишут разные издания: событие или дело вместе со всем, что из него выросло, — эпизодами, развитием, последствиями, реакциями и спорами вокруг. Тема может перерасти в следующий этап того же процесса (итоги выборов → учредительное заседание парламента → выбор председателя), пока о ней пишут без перерыва. Цепочка этапов кончается там, где начинается новый процесс со своим предметом: формирование правительства — своя тема, а не этап учредительного заседания парламента. Тема — не участник и не область: общий политик, партия, ведомство, компания, город или сфера («Вучич», «СНС», «прокуратура», «экономика», «погода») ещё не делают ситуацию одной.
- Одна тема: разные издания об одном событии; его развитие и последствия (подробности, обыски, задержания, суд, отставки, меры — крах банка, выплаты его вкладчикам и арест владельца — одна тема); реакции, заявления и споры именно о нём; эпизоды одного процесса — всё, что происходит на одном заседании, съезде, саммите, процессе в суде или в одной кампании (выступления, перепалки, инциденты, голосования, кандидатуры, трансляция заседания); подготовка, анонс и само событие; одни выборы — кампания, итоги, реакции.
- Разные темы: разные дела одного ведомства, компании или партии (прокуратура задержала одного и ответила на жалобу по другому делу); однотипные случаи без общего (две аварии, два ареста по разным делам, отключения в разных городах); выступления одного политика по несвязанным поводам; общая область без общей ситуации («Сербия и ЕС», «переговоры по Украине», «цены»); разные выборы (парламентские и в национальные советы меньшинств).
- Обзор нескольких событий относится к теме любого из них.
- Похожие слова ещё не значат одну тему."""

# как связаны новость и тема (перепроверка) или две темы (склейка): модель выбирает одно из LINKS, а не «да / нет» —
# на «одно ли это?» она отвечала «да» и для однотипного случая другого дня. Вместе — только KEEP_LINKS
LINKS = """- same — та же ситуация: то же событие, заседание, дело, процесс или кампания;
- development — следующий шаг или другой эпизод той же ситуации: подробности, задержания, обвинение, суд, итоги, выплаты, отставки по тому же делу; другое выступление, перепалка или голосование на том же заседании, съезде или процессе; следующий этап того же процесса; подготовка и анонс;
- consequence — прямое последствие одной ситуации в другой;
- reaction — заявление, ответ или спор именно о той ситуации;
- repeat — новый случай того же рода без общей ситуации: другая авария, другой арест, выступление по другому поводу, другая сводка ведомства;
- topic — общие только участник, ведомство, партия, компания, город или область;
- other — не связаны."""
KEEP_LINKS = ("same", "development", "consequence", "reaction")
LINK_ENUM = KEEP_LINKS + ("repeat", "topic", "other")

SYSTEM_PROMPT = f"""Ты раскладываешь свежие новости о Сербии по темам. {RULES}
Если сомневаешься — new: лишнюю тему потом склеят, а чужая новость в теме видна читателю.

Вход — JSON:
- stories: живые темы-кандидаты, id вида S…; name — название темы на сайте, если есть; event — ситуация темы одной фразой, если уже определена; whole — у большой темы: самые типичные её новости за всё время, её ситуация — вся тема по name и whole, а не первый эпизод; news — несколько новостей темы (что произошло), outlets — сколько изданий о ней пишут, updated — дата и время последней новости (UTC);
- news: новые новости, id вида N…: издание, дата и время, заголовок, что произошло (бывает пустым), candidates — темы, похожие по словам.
Заголовок бывает кликбейтом — опирайся на смысл.
join — только если новость о той же ситуации, что тема: о ней самой, её эпизоде, развитии, последствиях или реакциях именно на неё. Общий участник, ведомство или область — ещё не join: сверяй, о какой ситуации новость.

Для каждой новой новости верни verdict и target:
- join — о той же ситуации, что тема target (S… из её candidates); обзор, подходящий к нескольким candidates, — к той, у которой больше outlets, при равенстве — с более поздним updated;
- same — подходящей темы нет, но это та же ситуация, что другая новая новость target (N…);
- new — новая ситуация, target пустой;
- unclear — нельзя понять, о чём речь (кликбейт без факта), target пустой."""

SCHEMA = {
    "type": "object",
    "properties": {
        "decisions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "verdict": {"type": "string", "enum": ["join", "same", "new", "unclear"]},
                    "target": {"type": "string"},
                },
                "required": ["id", "verdict", "target"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["decisions"],
    "additionalProperties": False,
}


RECHECK_PROMPT = f"""Ты проверяешь, правильно ли новости разложены по темам агрегатора новостей о Сербии. {RULES}

Вход — JSON: stories — темы, id вида S…: event — ситуация темы, если уже определена, news — несколько новостей (что произошло); news — новости, id вида N…, каждая уже добавлена в тему story: time — когда пришла (UTC), title — заголовок, what — что произошло.
Для каждой новости сначала event — её повод одной фразой по самой новости: кто что сделал или что произошло и когда. Потом link — как этот повод связан с ситуацией темы (её event, а нет event — с тем, о чём её новости):
{LINKS}"""

RECHECK_SCHEMA = {
    "type": "object",
    "properties": {
        "news": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"id": {"type": "string"}, "event": {"type": "string"},
                               "link": {"type": "string", "enum": list(LINK_ENUM)}},
                "required": ["id", "event", "link"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["news"],
    "additionalProperties": False,
}
# join со сходством с сюжетом ниже порога Haiku перепроверяет на medium. Офлайн на 09–10.11: из 210 таких добавлений
# снял 9 из 19 чужих и 6 из 191 своих; Sonnet снимал больше чужих, но и своих столько же, а все добавления подряд —
# полнота падает сильнее, чем растёт точность
RECHECK_SIM = 0.9
# Перепроверяется и любой join в сюжет с карточкой, и любой join в сюжет со страницы (hot): 14.11 из 41 новости,
# которую проверка дня нашла не на месте после join, у 32 сходство было выше RECHECK_SIM — выступление Вучича на TV Pink
# легло в сюжет его вчерашнего обращения без перепроверки. Копии (COPY_SIM) не перепроверяются: они идут за оригиналом.
# Join в сюжет со страницы с 16.11 перепроверяет Sonnet (RECHECK_MODEL), остальные — Haiku: 15.11 Haiku оставил
# в сюжетах с карточкой и датой атаку дронов на три судна (карточка — на одно, днём раньше), новую сводку о семейном
# насилии, «Поштанску штедионицу» в сюжете о выплатах «Алта банки» — всё перепроверенное. Sonnet на все сюжеты
# с карточкой стоил бы $1.5–2 в день, на сюжеты страницы — около половины
# с 18.11 (темы) — Haiku: решение «та же ситуация?» проще, чем «то же событие?», а Sonnet стоил $0.25–0.8 в день
RECHECK_MODEL = MODEL
RECHECK_EFFORT = "medium"
# Sonnet перепроверяет join в сюжет страницы только со сходством ниже этого, выше — Haiku: перепроверка Sonnet стоила
# 16.11 $0.82 из $2.76 дня при бюджете $1.5. Из 414 join от 0.95 в сюжеты топа Sonnet снял 7 (1.7%), из 476 ниже — 34.
# Перепроверка в живой сборке — пакетом (stories_live): вдвое дешевле, снятая новость видна в чужом сюжете до сборки,
# в которую придёт ответ
RECHECK_SONNET_MAX = 0.95
# на странице — SHOW_TOP самых горячих видимых сюжетов (hot), и сильная модель работает на них: перепроверка Sonnet,
# склейка, вечерняя проверка. 15.11 в 30 самых больших сюжетах дня — 32% новостей и 45% добавлений в сюжет
SHOW_TOP = 30
# склейка видит у сюжета с карточкой меньше новостей: его повод задан карточкой
MERGE_SHOWN_CARD = 3
# горячесть сюжета: каждое издание даёт очко за свою последнюю новость в нём, очко вдвое тает за HEAT_HALF_LIFE.
# Свежий сюжет на 5 изданий выше вчерашнего на 15, одна реплика большой сюжет не поднимает
# До 16.11 — 6 часов, и к ночи главная история дня выпадала со страницы: 15.11 экономический план «Студенческого
# списка» (23 издания всех лагерей, почти все — вечером 14.11 и утром 15.11) днём был 3–7-м, а в 23:36 — 31-м,
# ниже протеста в Праге (7 изданий вечером). С 12 часами — 12-й, Прага — 27-я
HEAT_HALF_LIFE = timedelta(hours=12)
# сюжет о событии за рубежом без прямой связи с Сербией (карточка: serbia — foreign) горяч вчетверо меньше: в топе
# он только тогда, когда о нём пишут почти все. Касающееся Сербии (affects) — как сербское
FOREIGN_HEAT = 0.25
# поднять, когда правило связи с Сербией в CARD_PROMPT поменялось: живые сюжеты «foreign» будут спрошены заново
# (остальные отметки не трогаем — правило их только расширяло). 2 — с 16.11: Косово и косовские политики где угодно,
# сербские организации и СМИ — serbia; первая разметка отправила приговор Тачи в Гааге в foreign
SERBIA_MARKS = 2
# регулярная сводка (ночная сводка скорой помощи Белграда, ДТП без погибших, прогноз погоды, пробки, плановые
# отключения — карточка: routine) горяча вчетверо меньше, как зарубежное: каждое утро 8–10 изданий перепечатывают
# сводку Hitna pomoć, и до 23.11 она стояла в топе выше политики. Со страницы не пропадает — только ниже
ROUTINE_HEAT = 0.25

MERGE_PROMPT = f"""Ты проверяешь темы агрегатора новостей о Сербии на дубли. {RULES}

Вход — JSON: stories — темы, id вида S…: name — название темы на сайте, если есть; event — ситуация темы, если уже определена; whole — у большой темы: самые типичные её новости за всё время; news — несколько новостей (дата время UTC · издание: что произошло); pairs — пары тем, похожих по словам или с одинаковым названием, id вида P…: a и b — темы пары.
Для каждой пары сначала event_a — ситуация темы a. Есть у темы event — это она: оставь event_a пустой строкой. Нет event — назови ситуацию одной фразой по name, whole и news: что происходит, где, с кем; у большой темы — вся её ситуация, а не первый или последний эпизод. Потом так же event_b — ситуация темы b.
Потом link — как связаны ситуации:
{LINKS}
Перед same, development, consequence или reaction сверь, об одной ли ситуации речь: то же заседание, то же дело, тот же процесс в суде, те же вкладчики того же банка, та же кампания. Общие люди, ведомство, прокуратура, партия, город или день без общей ситуации — topic. Так, topic или repeat:
- задержание деканов факультета и обыск в пенсионном фонде по приказу той же прокуратуры;
- выплаты вкладчикам рухнувшего банка и продажа подразделения того же банка за рубежом;
- угрозы родителей одной школе и заявление профсоюза другого города против того же министра из-за другой школы;
- выступление политика на заседании парламента и его же интервью о другом деле.
А так — одна тема: выбор председателя парламента, речи и перепалки на том же заседании и перенос его трансляции; крах банка, выплаты вкладчикам и арест владельца.
Если сомневаешься — topic."""

# повод каждого сюжета пары модель называет отдельно (event_a, event_b), до link. С 13.11 она называла одно общее
# событие — и 14.11 в 68 из 92 склеек соединяла через «и» два повода («обращение в соцсетях и выступление на TV Pink»),
# так что склеек по общему участнику стало только больше. Сходство их не отличает: у неверных 0.87–0.91, у верных
# столько же. Поводы пишутся в журнал склеек и на страницу. Повод сюжета с карточкой — сама карточка (merge_answer),
# модель его не пишет: 15.11 она «пересказала» карточку s5648 о решении пленума распустить БИА как «пленум решил…,
# 15.11 Вучевич ответил…» — двумя поводами через запятую, по чужим новостям сюжета — и дважды приклеила к нему ответ
# Вучевича Лазовичу. Заодно ответ вдвое короче: 20 пар с двумя поводами — 2 тыс. токенов
# С 18.11 development / consequence / reaction — только при прямой причине или том же деле, с контрпримерами:
# проверка дня 17.11 нашла 138 лишних новостей из 2427, 95 — от склейки, почти все с общим участником или ведомством,
# названные «развитием» (Алта банка и кредиты родне главы агентства, призыв Группы 46 и вызов Ускоковича в полицию, гимназия
# в Нови-Саде и профсоюз Крагуеваца). Haiku 17.11 назвал topic 1078 пар, Sonnet в 224 из них увидел development
MERGE_SCHEMA = {
    "type": "object",
    "properties": {
        "pairs": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"id": {"type": "string"}, "event_a": {"type": "string"}, "event_b": {"type": "string"},
                               "link": {"type": "string", "enum": list(LINK_ENUM)}},
                "required": ["id", "event_a", "event_b", "link"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["pairs"],
    "additionalProperties": False,
}
# склейка раз в 2 часа (до 14.11 — раз в час): читателей пока мало, дубль может подождать, а проход стоит денег.
# Когда склеивать — не правило раскладки, в отпечаток не входит (stories_live.NOT_RULES)
MERGE_EVERY = timedelta(hours=2)
MERGE_SIM = 0.865  # пары сюжетов с лучшей парой новостей не ниже — к модели (у MiniLM было 0.55)
# одиночка (сюжет из одного издания) с видимым — с тем же порогом, что при раскладке: новость, пришедшая раньше своего
# сюжета, проверяется так же, как пришедшая после. 12.11 ремонт JANAF (0.864) остался одиночкой рядом с сюжетом о нём
MERGE_FRAGMENT_SIM = CANDIDATE_SIM
MERGE_BATCH = 20  # пар в одном запросе
# склейку решает Sonnet. Фильтр Haiku перед ним с 12.11 до 16.11 не стоял: с крупными сюжетами Haiku стал строже
# Sonnet — из 139 пар, которые Sonnet склеил из 357 прежних отказов, пропустил бы только 78 (тогда Haiku отвечал
# «да / нет»). С 17.11 он снова первый, но отсеивает только пары, которые назвал не связанными (MERGE_FILTER_DROP):
# 16.11 Sonnet спросили о 1992 парах, 834 он назвал other, 847 — topic, и склейка стоила $0.92 из $2.76 дня.
# Каждую MERGE_CONTROL-ю отсеянную пару Sonnet всё равно видит: state["merge_stats"] — как ответили оба, «other>same» —
# склейки, которые фильтр теряет. Теряет мало — можно отсеивать и topic
# с 18.11 (темы) склейку решает Haiku, без фильтра перед ним (merge_filter): склейка Sonnet стоила ~$1 в день
MERGE_MODEL = MODEL
MERGE_FILTER_MODEL = MODEL
MERGE_FILTER_DROP = ("other",)
MERGE_CONTROL = 10
MERGE_EFFORT = "medium"
MERGE_SHOWN = 5  # сколько новостей каждого сюжета видит модель при склейке
# большая тема (от BIG новостей): её карточка не переписывается и остаётся про первый эпизод — склейка и раскладка видят вместо неё
# название и WHOLE_SHOWN самых типичных новостей из WHOLE_POOL, взятых равномерно за всё время. 24.11 s12970 (196 новостей
# учредительного заседания с 21.11) с карточкой «Заявления Вучича о противниках… и воде» склейка сочла другой темой,
# чем s13840 (консультации о комитетах того же заседания), — и обе висели в топе как «Учредительное заседание Скупщины Сербии»
BIG = 60
WHOLE_POOL = 300
WHOLE_SHOWN = 6
MERGE_SURE_SIM = 0.9  # пары похожее этого идут к модели и без общих участников (у MiniLM было 0.7)
SONNET = "claude-sonnet-5-5"
PRICES = {MODEL: (0.10, 0.50), SONNET: (2.0, 10.0)}  # вход / выход за 1M токенов, $


# отдельные события внутри больших сюжетов — в свои сюжеты (13.11: из 8 предложений за день верны все 8)
LINES_PROMPT = """Ты проверяешь большие сюжеты агрегатора новостей о Сербии. Сюжет собирает одно событие или одно дело вместе с его развитием, последствиями и реакциями. Разрастаясь, он может вобрать отдельное событие: у него свой повод и о нём пишут как о самостоятельном, даже если участники те же.
Вход — JSON: event — основное событие сюжета, если определено; news — новости сюжета по времени: n — номер, t — время UTC, source — издание, what — что произошло.
Верни lines — отдельные события внутри сюжета: name — 1–3 слова по-русски, why — одной фразой, чем это отдельное событие, news — номера n всех его новостей. Подробности, реакции, заявления и следствия без своего повода — не отдельное событие. Основное событие сюжета в lines не входит. Если такого нет или сомневаешься — пустой lines."""
LINES_SCHEMA = {
    "type": "object",
    "properties": {
        "lines": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"name": {"type": "string"}, "why": {"type": "string"},
                               "news": {"type": "array", "items": {"type": "integer"}}},
                "required": ["name", "why", "news"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["lines"],
    "additionalProperties": False,
}
LINES_MIN = 15  # сюжет проверяем, когда в нём столько новостей,
# и снова — когда в нём прибавилось столько новостей (до 13.11 — когда вырос в 1.5 раза: 156 новостей s3868
# ждали проверки до 234). Когда проверять — не правило раскладки, в отпечаток не входит (stories_live.fingerprint)
# 15 до 17.11: деление стоило $0.28 в день, а бюджет сюжетов — $1.5
LINES_STEP = 30
LINES_NEWS, LINES_OUTLETS = 3, 2  # отдельное событие — не меньше стольких новостей и изданий; и столько же остаётся
LINES_EFFORT = "medium"
# деление больших сюжетов на отдельные события — до 18.11; у тем эпизоды — норма, деления нет
LINES_ON = False

# карточка события: видимому сюжету — один раз, когда о нём написало второе издание, одной фразой: кто, что, когда.
# Дальше она не меняется: раскладка и склейка сверяют новости с ней, а не с тем, какие новости в сюжете сейчас.
# Без неё сюжет уплывал: 14.11 s4048 начался программой «Студенческого списка», вобрал обращение Вучича, потом его
# выступление на TV Pink — и новости о программе стали в нём чужими. Карточка — у сюжета с этим id и после склейки:
# unmerge вернёт её вместе с сюжетом
CARD_PROMPT = f"""Ты заводишь карточки тем агрегатора новостей о Сербии. {RULES}
Вход — JSON: stories — темы, id вида S…, news — их новости (дата время UTC · издание: что произошло); event — если у темы карточка уже есть.
Для каждой верни event — ситуацию темы одной фразой: что происходит сейчас, где и с кем, по последним новостям. Не один эпизод, а ситуация целиком («учредительное заседание Скупщины Сербии и выбор председателя», а не «Вучич выступил на заседании»). Без оценок и без подробностей, которых нет в новостях. Если новости темы о разных ситуациях — та, о которой больше новостей. Если event уже дан — верни его как есть.
Потом serbia — связь события с Сербией:
- serbia — событие в Сербии или в Косове и Метохии; или его участники — сербские или косовские государство, политики, партии, организации, СМИ, компании, клубы, сборные, граждане, где бы это ни было: суд в Гааге над косовскими политиками, переговоры Белграда и Приштины в Брюсселе, выступление сербской сборной за рубежом — тоже serbia;
- affects — событие за рубежом, которое прямо касается Сербии или сербов: решения ЕС, США, России, соседей о Сербии; сербы в Республике Сербской, Черногории, Хорватии; санкции, цены, перебои, эпидемия или кризис, которые доходят до Сербии;
- foreign — событие за рубежом без прямой связи с Сербией, даже если о нём пишут сербские издания и даже если оно похоже на сербские дела: протесты, выборы, коррупция, происшествия в других странах.
И routine — true, если тема — регулярная сводка, которая выходит почти каждый день и ничего особенного не сообщает: сводка скорой помощи или полиции за ночь, обычные ДТП и травмы без погибших, прогноз погоды и обычная погода, пробки и обстановка на границах, плановые отключения воды, света и тепла, курс валют и цены на топливо по графику. false — всё остальное, и сводка тоже, если в ней необычное: погибшие, массовая авария, крупный пожар, стихийное бедствие, непогода с последствиями, большие перебои."""
CARD_SCHEMA = {
    "type": "object",
    "properties": {
        "cards": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"id": {"type": "string"}, "event": {"type": "string"},
                               "serbia": {"type": "string", "enum": ["serbia", "affects", "foreign"]},
                               "routine": {"type": "boolean"}},
                "required": ["id", "event", "serbia", "routine"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["cards"],
    "additionalProperties": False,
}
CARD_EFFORT = "medium"
CARD_BATCH = 20
CARD_SHOWN = 4  # сколько новостей видит модель: при первой карточке их в сюжете обычно 2–3


def eligible(item: dict) -> bool:
    """Идёт ли новость в сюжеты: события (и новости без kind — русскоязычные до 13.11), без Reddit и Trends."""
    return item["section"] not in SKIP_SECTIONS and item.get("kind") in ("event", None)


def text_of(item: dict) -> str:
    ru = item.get("ru") or {}
    return item.get("what") or ru.get("title") or item["title"]


def embed(texts: list[str]) -> np.ndarray:
    """Векторы единичной длины. Модель скачивается при первом вызове (~2 ГБ) и дальше берётся из кэша."""
    from fastembed import TextEmbedding

    global _embedder
    if "_embedder" not in globals():
        _embedder = TextEmbedding(EMBED_MODEL)
    vectors = np.array(list(_embedder.embed([EMBED_PREFIX + t for t in texts], batch_size=64)), dtype=np.float32)
    return vectors / np.linalg.norm(vectors, axis=1, keepdims=True)


def new_state() -> dict:
    return {"stories": {}, "items": {}, "next_id": 0}


def request(system: str, content: str, schema: dict, effort: str = EFFORT, model: str = MODEL) -> dict:
    """Параметры запроса к модели — и для обычного, и для пакетного. Системный промпт — в кэше, как у перевода."""
    return {"model": model, "max_tokens": 16000, "system": translate.cached(system),
            "output_config": {"effort": effort, "format": {"type": "json_schema", "schema": schema}},
            "messages": [{"role": "user", "content": content}]}


def answer_of(message, price: tuple[float, float]) -> tuple[dict, dict]:
    """Ответ модели: (ответ по схеме, {"in", "out", "write", "read", "usd"} — токены, из них записанные в кэш
    и прочитанные из кэша, и оценка в долларах по price — вход / выход за 1M токенов)."""
    if message.stop_reason in ("max_tokens", "refusal"):
        raise RuntimeError(f"stop_reason={message.stop_reason}")
    text = next(b.text for b in message.content if b.type == "text")
    used = translate.tokens(message.usage)
    return json.loads(text), {**used, "usd": translate.usd(used, price)}


def ask_model(client, system: str, content: str, schema: dict, effort: str = EFFORT,
              model: str = MODEL, price: tuple[float, float] | None = None) -> tuple[dict, dict]:
    """Запрос к модели сразу: (ответ по схеме, токены и доллары — answer_of). price — цена модели, которой нет
    в PRICES (вход / выход за 1M токенов)."""
    return answer_of(client.messages.create(**request(system, content, schema, effort, model)), price or PRICES[model])


def visible(state: dict, sid: str) -> bool:
    return len({state["items"][u]["source"] for u in state["stories"][sid]["urls"]}) >= 2


def _start(state: dict, now: str) -> str:
    sid = f"s{state['next_id']}"
    state["next_id"] += 1
    state["stories"][sid] = {"created": now, "updated": now, "urls": []}
    return sid


def _add(state: dict, sid: str, url: str, item: dict, verdict: str, now: str, part: str | None = None,
         sim: float | None = None) -> None:
    """part — часть сюжета (событие); по умолчанию — сам сюжет: новость его открыла. sim — сходство с сюжетом (join).
    Новость уже была (перепроверка сняла её из сюжета) — история сохраняется, шаг пишется с from."""
    ru = item.get("ru") or {}
    prev = state["items"].get(url)
    n = (prev or {}).get("n")
    if n is None:  # короткий номер новости — для команд боту (stories_live.py): «/move 1234 s471»
        n = state.get("next_n", 1)
        state["next_n"] = n + 1
    state["items"][url] = {"n": n, "story": sid, "part": part or sid, "verdict": verdict, "seen": now, "source": item["source"],
                           "section": item["section"], "title": ru.get("title") or item["title"],
                           "what": item.get("what", ""), "entities": item.get("entities") or [],
                           "history": (prev or {}).get("history", [])}
    step = {"t": now, "why": verdict, "story": sid, "part": part or sid}
    if sim is not None:
        step["sim"] = round(sim, 3)
    if prev:
        step["from"] = prev["story"]
    state["items"][url]["history"].append(step)
    if sid:
        state["stories"][sid]["urls"].append(url)
        state["stories"][sid]["updated"] = now


def _live(state: dict, now: str) -> tuple[list[str], list[str]]:
    """Живые сюжеты: (адреса их последних новостей, id сюжета для каждого адреса)."""
    cutoff = (datetime.fromisoformat(now) - LIVE).isoformat()
    urls, owners = [], []
    for sid, story in state["stories"].items():
        if story["updated"] >= cutoff:
            for url in story["urls"][-STORY_ITEMS:]:
                urls.append(url)
                owners.append(sid)
    return urls, owners


def _decide(state: dict, batch: list[tuple[str, dict]], now: str, vectors: dict, ask, later: bool = False) -> dict:
    """Одна пачка новых новостей: кандидаты, запрос к модели, решения в состояние. later — перепроверку не спрашивать
    сразу, а сложить в state["recheck_queue"]. Возвращает токены."""
    live_urls, owners = _live(state, now)
    new_vecs = np.stack([vectors[url] for url, _ in batch])
    candidates = [{} for _ in batch]  # {id сюжета: лучшее сходство с его новостями}
    closest = {}  # id сюжета → его новости, самые похожие на новые новости, для которых он кандидат
    copies = {}  # новая новость → (уже разложенная, которую она повторяет, сходство): идёт туда же (COPY_SIM)
    if live_urls:
        sims = new_vecs @ np.stack([vectors[u] for u in live_urls]).T
        for n in range(len(batch)):
            if sims[n].max() >= COPY_SIM:
                copies[n] = (live_urls[int(sims[n].argmax())], float(sims[n].max()))
                continue
            for k in np.argsort(-sims[n]):
                if sims[n, k] < CANDIDATE_SIM or len(candidates[n]) == CANDIDATES:
                    break
                if owners[k] not in candidates[n]:
                    candidates[n][owners[k]] = float(sims[n, k])
                    closest.setdefault(owners[k], []).append(live_urls[k])
    # соседи по сборке: если две новые новости похожи, обе идут к модели — вдруг это новое событие
    near = new_vecs @ new_vecs.T
    np.fill_diagonal(near, 0)
    asked = [n for n in range(len(batch)) if n not in copies and (candidates[n] or near[n].max(initial=0) >= CANDIDATE_SIM)]
    for n in range(len(batch)):
        url, item = batch[n]
        if n in copies:
            original, sim = state["items"][copies[n][0]], copies[n][1]
            _add(state, original["story"], url, item, "copy", now, original["part"], sim)
            state["items"][url]["history"][-1]["of"] = original["n"]
        elif n not in asked:
            _add(state, _start(state, now), url, item, "alone", now)
    if not asked:
        return {"in": 0, "out": 0}

    aliases = {}  # S0, S1… → id сюжета: короткие id модель не путает
    for n in asked:
        for sid in candidates[n]:
            aliases.setdefault(sid, f"S{len(aliases)}")
    stories, names = [], state.get("names", {})
    spun = state.get("spun", {})  # выделенный сюжет → из какого (find_lines)
    for sid, alias in aliases.items():
        # название и, у большой темы, вся тема вместо застывшей первой карточки (merge_event): 24.11 пресс-конференция
        # «Студенческого списка» после заседания Скупщины открыла свою тему s14914 — s13840 (всё заседание) раскладка
        # видела как «консультации по формированию комитетов»
        stories.append({"id": alias}
                       | ({"name": names[sid]} if sid in names else {})
                       | ({"event": merge_event(state, sid)} if merge_event(state, sid) else {})
                       | ({"whole": whole(state, vectors, sid)} if is_big(state, sid) else {})
                       | {"news": _shown(state, sid, vectors, extra=closest.get(sid, [])),
                          "outlets": len({state["items"][u]["source"] for u in state["stories"][sid]["urls"]}),
                          "updated": _when(state["stories"][sid]["updated"])}
                       | ({"from": aliases[spun[sid]]} if spun.get(sid) in aliases else {}))
    news = []
    for n in asked:
        url, item = batch[n]
        ru = item.get("ru") or {}
        news.append({"id": f"N{n}", "source": item["source"], "time": _when(now),
                     "title": ru.get("title") or item["title"], "what": item.get("what", ""),
                     "candidates": [aliases[s] for s in candidates[n]]})
    content = json.dumps({"stories": stories, "news": news}, ensure_ascii=False)
    answer, usage = ask(SYSTEM_PROMPT, content, SCHEMA)

    by_alias = {alias: sid for sid, alias in aliases.items()}
    verdicts = {d["id"]: d for d in answer["decisions"]}
    weak = []  # (n, alias сюжета, модель): join со слабым сходством, в сюжет страницы или с карточкой — на перепроверку
    page = set(hot(state, now))
    for n in asked:  # по порядку: «same» ссылается на более раннюю новость, у которой сюжет уже есть
        url, item = batch[n]
        d = verdicts.get(f"N{n}", {"verdict": "new", "target": ""})
        verdict, target = d["verdict"], d["target"]
        part = sim = None
        if verdict == "join" and target in by_alias:  # можно и чужого кандидата из той же пачки: модель видела его
            sid = by_alias[target]
            # сюжет, выделенный из выбранного, похож на новость больше — значит, она о выделенном событии: 15.11
            # «Вучич возглавил фракцию» (0.975 к выделенному, 0.891 к старому) модель положила в старый сюжет,
            # потому что там осталась одна новость о том же
            while child := max((c for c in candidates[n] if spun.get(c) == sid and candidates[n][c] > candidates[n].get(sid, 0)),
                               key=candidates[n].get, default=None):
                sid, target = child, aliases[child]
            part = _nearest_part(state, sid, vectors[url], vectors)
            sim = candidates[n].get(sid)
            # с 18.11 join в тему с карточкой сам по себе не повод перепроверять: у тем эпизоды — норма
            if candidates[n].get(sid, 0) < RECHECK_SIM or sid in page:
                strong = sid in page and candidates[n].get(sid, 0) < RECHECK_SONNET_MAX
                weak.append((n, target, RECHECK_MODEL if strong else MODEL))
        elif verdict == "same" and target.startswith("N") and target[1:].isdigit() \
                and batch[int(target[1:])][0] in state["items"] and state["items"][batch[int(target[1:])][0]]["story"]:
            sid = state["items"][batch[int(target[1:])][0]]["story"]
            part = state["items"][batch[int(target[1:])][0]]["part"]
        else:  # и unclear: без сюжета до неё не дошла бы склейка, а 12.11 «непонятным» стал внятный анонс матча
            verdict = "new" if verdict in ("join", "same", "new") else verdict
            sid = _start(state, now)
        _add(state, sid, url, item, verdict, now, part, sim)
    if weak:
        requests = _recheck_requests(state, batch, weak, stories, now)
        if later:  # пакетом — отправит и применит stories_live
            state.setdefault("recheck_queue", []).extend(requests)
        else:
            for r in requests:
                answer, used = ask(RECHECK_PROMPT, r["content"], RECHECK_SCHEMA, RECHECK_EFFORT, r["model"])
                recheck_apply(state, r["news"], answer, now)
                usage = {k: usage.get(k, 0) + v for k, v in used.items()}
    return usage


def heat(state: dict, sid: str, now: str) -> float:
    latest = {}  # издание → время его последней новости в сюжете (публикации, не позже, чем увидели)
    for u in state["stories"][sid]["urls"]:
        it = state["items"][u]
        latest[it["source"]] = max(latest.get(it["source"], ""), it.get("published", it["seen"]))
    moment = datetime.fromisoformat(now)
    weight = FOREIGN_HEAT if state.get("serbia", {}).get(sid) == "foreign" else 1
    weight *= ROUTINE_HEAT if state.get("routine", {}).get(sid) else 1
    return weight * sum(0.5 ** ((moment - datetime.fromisoformat(t)) / HEAT_HALF_LIFE) for t in latest.values())


def hot(state: dict, now: str) -> list[str]:
    """Сюжеты страницы: SHOW_TOP самых горячих видимых живых сюжетов, по убыванию горячести."""
    cutoff = (datetime.fromisoformat(now) - LIVE).isoformat()
    heats = {sid: heat(state, sid, now) for sid, s in state["stories"].items() if s["updated"] >= cutoff and visible(state, sid)}
    return sorted(heats, key=lambda sid: (heats[sid], state["stories"][sid]["updated"]), reverse=True)[:SHOW_TOP]


def _when(iso: str) -> str:
    """Дата и время UTC для модели: «14.11 18:30» — без даты выступление сегодня и вчера неотличимы."""
    return f"{iso[8:10]}.{iso[5:7]} {iso[11:16]}"


def cards(state: dict, vectors: dict, ask, now: str) -> int:
    """Карточки событий видимым сюжетам, у которых их ещё нет, и связь сюжета с Сербией (state["serbia"]: serbia,
    affects, foreign) и регулярная ли это сводка (state["routine"]) — Haiku, пачками. Живым сюжетам с карточкой, но без
    отметки (до 16.11 — связи, до 23.11 — сводки) — только отметки: карточка остаётся. Возвращает, скольких спросили."""
    have, marks = state.setdefault("cards", {}), state.setdefault("serbia", {})
    routine = state.setdefault("routine", {})
    if state.get("serbia_marks", 1) != SERBIA_MARKS:
        for sid in [sid for sid, mark in marks.items() if mark == "foreign"]:
            del marks[sid]
        state["serbia_marks"] = SERBIA_MARKS
    cutoff = (datetime.fromisoformat(now) - LIVE).isoformat()
    todo = [sid for sid, story in state["stories"].items() if visible(state, sid)
            and (sid not in have or (sid not in marks or sid not in routine) and story["updated"] >= cutoff)]
    for start in range(0, len(todo), CARD_BATCH):
        chunk = todo[start:start + CARD_BATCH]
        content = json.dumps({"stories": [{"id": f"S{n}"} | ({"event": have[sid]} if sid in have else {})
                                          | {"news": _shown(state, sid, vectors, CARD_SHOWN, detail=True)}
                                          for n, sid in enumerate(chunk)]}, ensure_ascii=False)
        answer, _ = ask(CARD_PROMPT, content, CARD_SCHEMA, CARD_EFFORT, MODEL)
        for card in answer["cards"]:
            n = card["id"][1:]
            if n.isdigit() and int(n) < len(chunk) and card["event"].strip():
                have.setdefault(chunk[int(n)], card["event"].strip())
                marks[chunk[int(n)]] = card["serbia"]
                routine[chunk[int(n)]] = card["routine"]
    return len(todo)


def _nearest_part(state: dict, sid: str, vector: np.ndarray, vectors: dict) -> str:
    """Часть сюжета, в которой самая похожая на новость новость (среди последних STORY_ITEMS)."""
    urls = [u for u in state["stories"][sid]["urls"][-STORY_ITEMS:] if u in vectors]
    if not urls:
        return sid
    nearest = state["items"][urls[int((np.stack([vectors[u] for u in urls]) @ vector).argmax())]]
    return nearest.get("part") or nearest["story"]


def _recheck_requests(state: dict, batch: list[tuple[str, dict]], weak: list[tuple[int, str, str]],
                      stories: list[dict], now: str) -> list[dict]:
    """Запросы перепроверки join со слабым сходством, в сюжет страницы и в сюжет с карточкой: сюжет — те же новости,
    что модель видела при раскладке; weak — (номер новости, сюжет, какая модель проверяет). Запрос — на каждую модель:
    {"model", "content", "news": [[id N… в запросе, адрес новости, сюжет, в который она легла]]}."""
    shown = {s["id"]: s for s in stories}
    requests = []
    for model in dict.fromkeys(m for *_, m in weak):
        mine = [(n, a) for n, a, m in weak if m == model]
        content = json.dumps({"stories": [shown[a] for a in dict.fromkeys(a for _, a in mine)],
                              "news": [{"id": f"N{n}", "story": a, "time": _when(now), "title": state["items"][batch[n][0]]["title"],
                                        "what": batch[n][1].get("what", "")} for n, a in mine]}, ensure_ascii=False)
        requests.append({"model": model, "content": content,
                         "news": [[f"N{n}", batch[n][0], state["items"][batch[n][0]]["story"]] for n, _ in mine]})
    return requests


def recheck_apply(state: dict, news: list[list[str]], answer: dict, now: str) -> int:
    """Ответ на запрос перепроверки (news — его новости, как в _recheck_requests). Модель называет повод новости
    и как он связан с событием сюжета (LINKS); связь не из KEEP_LINKS — новость уходит в свой сюжет (verdict recheck,
    в истории — повод и связь), если она всё ещё там, куда её положила раскладка: ответ пакета приходит через сборку,
    а за это время её могли склеить или перенести руками. Копии, пришедшие за ней тем временем, уходят с ней; новости,
    легшие к ней через same, остаются где были. Возвращает, сколько снято."""
    drop = {d["id"]: d for d in answer["news"] if d["link"] not in KEEP_LINKS}
    checked, items = state.setdefault("checked", {}), state["items"]
    taken = 0
    for nid, url, old in news:
        it = items.get(url)
        if nid not in drop or not it or it["story"] != old:
            continue
        copies = [u for u in state["stories"][old]["urls"]
                  if items[u].get("history") and items[u]["history"][-1]["why"] == "copy" and items[u]["history"][-1].get("of") == it["n"]]
        new = move(state, url, None, now, why="recheck")
        it["verdict"] = "recheck"
        it["history"][-1] |= {"link": drop[nid]["link"], "event": drop[nid]["event"].strip()}
        for u in copies:
            move(state, u, new, now, part=new, why="copy")
            items[u]["history"][-1]["of"] = it["n"]
        taken += 1
        # пара «откуда сняли — куда» для склейки уже проверена как разная, пока один из сюжетов не вырастет вдвое:
        # 15.11 перепроверка сняла 18810 (Вучевич о запрете СНС) с сюжета о решении пленума распустить БИА,
        # а склейка через полчаса вернула её туда вместе с новым сюжетом. Рост нового сюжета пару не открывает (0):
        # он растёт копиями того же повода, и с 1 новости «вдвое» — это вторая. 16.11 перепроверка сняла протест
        # SMER (19866) с сюжета о назначении Ускоковича как topic, а склейка через полтора часа вернула его туда
        if old in state["stories"]:
            checked[f"{old}|{new}"] = [len(state["stories"][old]["urls"]), 0]
    return taken


def _shown(state: dict, sid: str, vectors: dict, count: int = SHOWN_ITEMS, detail: bool = False,
           extra: list[str] = ()) -> list[str]:
    """Что модель видит о сюжете: первая новость и самые типичные — похожие на остальные новости сюжета больше всех,
    по порядку появления, без повторов текста. Не последние: с последними одна чужая новость, попав в сюжет,
    становится его лицом и тянет за собой следующие (10.11 так в сюжет решения REM легли 19 новостей
    про обыск у Вучичевича). detail — с временем (UTC) и изданием: склейке без времени «удар по Киеву» и «удар
    по Украине» в ту же ночь казались разными событиями (12.11). extra — ещё новости сюжета: при раскладке — самые похожие
    на новую новость. Типичные показывают главную тему, и поворот того же события на неё не похож: 12.11 призыв
    Брнабич в Дублине быстрее сформировать институты лёг отдельно от сюжета о саммите, хотя такие же новости в нём были."""
    urls = state["stories"][sid]["urls"][-STORY_ITEMS:]
    first = state["stories"][sid]["urls"][0]
    rest = [u for u in urls if u != first]
    if len(rest) > count - 1:
        v = np.stack([vectors[u] for u in urls])
        typical = (v @ v.T).mean(axis=1)[[urls.index(u) for u in rest]]
        keep = set(np.argsort(-typical)[:count - 1])
        rest = [u for n, u in enumerate(rest) if n in keep]
    order = state["stories"][sid]["urls"]
    chosen = sorted(set([first] + rest + list(extra)), key=order.index)
    items = [state["items"][u] for u in chosen]
    if detail:
        return list(dict.fromkeys(f"{it['seen'][8:10]}.{it['seen'][5:7]} {it['seen'][11:16]} · {it['source']}: {text_of(it)}"
                                  for it in items))
    return list(dict.fromkeys(text_of(it) for it in items))


# участники приходят и по-русски, и по-сербски, кириллицей и латиницей: сравниваем латиницей без диакритики —
# «ЯНАФ» и «JANAF», «Шошкич» и «Šoškić» совпадут (12.11 ремонт ЯНАФ не склеился с сюжетом о JANAF)
TRANSLIT = str.maketrans({**dict(zip("абвгдезиклмнопрстуфхцчшыэ", "abvgdeziklmnoprstufhccsye")),
                          "ё": "e", "ж": "z", "й": "j", "щ": "s", "ъ": "", "ь": "", "ю": "ju", "я": "ja",
                          "ђ": "dj", "ј": "j", "љ": "lj", "њ": "nj", "ћ": "c", "џ": "dz",
                          "č": "c", "ć": "c", "š": "s", "ž": "z", "đ": "dj"})


def _keys(entities: list[str]) -> set[str]:
    """Участники для сравнения: имя целиком и последнее слово («Деян Шошкич» и «Шошкич» совпадут), латиницей."""
    keys = set()
    for e in entities:
        e = e.lower().strip().translate(TRANSLIT)
        keys.add(e)
        last = e.replace("«", " ").replace("»", " ").split()[-1:] if e else []
        if last and len(last[0]) >= 4:
            keys.add(last[0])
    return keys


def _share_entities(state: dict, a: str, b: str) -> bool:
    """Есть ли у сюжетов общий участник. У кого участников нет совсем (русскоязычные ленты) — не отсеиваем."""
    ka, kb = ({k for u in state["stories"][s]["urls"] for k in _keys(state["items"][u].get("entities") or [])}
              for s in (a, b))
    return not ka or not kb or bool(ka & kb)


def merge_todo(state: dict, now: str, vectors: dict) -> list[tuple[str, str]]:
    """Пары сюжетов для склейки. Пара идёт к модели, только если: хотя бы один сюжет на странице (hot: дубли и обрывки
    остальных читатель не видит; с 16.11, до того — хотя бы один видимый; невидимый рядом с видимым — новость, которая
    должна была лечь в видимый: пришла раньше него или модель ошиблась), хотя бы один вырос с прошлой склейки, лучшая пара новостей похожа не меньше MERGE_SIM
    (одиночка с видимым — MERGE_FRAGMENT_SIM), у сюжетов есть общий участник (entities; не нужен, если сходство не ниже
    MERGE_SURE_SIM), пару ещё не признавали разными — или с тех пор один из сюжетов вырос вдвое — и её части не
    разделяли руками (split, unmerge). Отмечает время склейки: следующая — через MERGE_EVERY."""
    stories, checked = state["stories"], state.setdefault("checked", {})
    last = state.get("merged_at") or ""
    again = set(state.pop("merge_again", []))  # склеенные в прошлый проход: их пары с соседями ещё не проверены
    live_urls, owners = _live(state, now)
    shown = {sid for sid in set(owners) if visible(state, sid)}
    page = set(hot(state, now))
    state["merged_at"] = now
    if len(live_urls) < 2:
        return []
    sims = np.stack([vectors[u] for u in live_urls])
    sims = sims @ sims.T
    rows, cols = np.nonzero(np.triu(sims, 1) >= min(MERGE_SIM, MERGE_FRAGMENT_SIM))
    candidates = {}
    for r, c in zip(rows, cols):
        a, b = sorted((owners[r], owners[c]), key=lambda s: int(s[1:]))
        both = a in shown and b in shown
        if a != b and (a in page or b in page) and sims[r, c] >= (MERGE_SIM if both else MERGE_FRAGMENT_SIM):
            candidates[(a, b)] = max(candidates.get((a, b), 0), sims[r, c])
    todo = []
    for (a, b), sim in sorted(candidates.items()):
        grew = stories[a]["updated"] >= last or stories[b]["updated"] >= last or a in again or b in again
        if not grew or _apart(state, a, b):
            continue
        old = checked.get(f"{a}|{b}")
        if old and (old[2:] == [1] or len(stories[a]["urls"]) < 2 * old[0]
                    and (not old[1] or len(stories[b]["urls"]) < 2 * old[1])):
            continue
        if sim >= MERGE_SURE_SIM or _share_entities(state, a, b):
            todo.append((a, b))
    return todo + _same_names(state, page, set(owners), set(todo))


def _same_names(state: dict, page: set[str], live: set[str], todo: set[tuple[str, str]]) -> list[tuple[str, str]]:
    """Живые темы с одинаковым названием, хотя бы одна на странице, — к склейке сразу: без сходства, общих участников и
    ожидания, пока одна вырастет вдвое. Одинаковое название — почти наверняка одна ситуация (24.11 две темы
    «Учредительное заседание Скупщины Сербии» в топе). Признанные разными с тем же названием — снова не спрашиваем
    (state["checked_names"]), пока одна из них не сменит название."""
    names, asked = state.get("names", {}), state.setdefault("checked_names", {})
    by_name = {}
    for sid in sorted(live, key=lambda s: int(s[1:])):
        if sid in names:
            by_name.setdefault(names[sid].strip().lower(), []).append(sid)
    pairs = []
    for name, sids in by_name.items():
        for i, a in enumerate(sids):
            for b in sids[i + 1:]:
                key = f"{a}|{b}"
                if (a in page or b in page) and (a, b) not in todo and asked.get(key) != name and not _apart(state, a, b):
                    asked[key] = name
                    pairs.append((a, b))
    return pairs


def _mark_checked(state: dict, a: str, b: str) -> None:
    """Пара признана разной: state["checked"]["a|b"] — [размер a, размер b, 1 — если у обоих карточки]. Пару сюжетов
    с карточками склейка больше не спрашивает: их поводы — карточки, а карточка не меняется, сколько бы сюжет ни вырос.
    16.11 повторные вопросы о таких парах — около 500 из 1992. Остальные — снова, когда один из сюжетов вырос вдвое."""
    # с 18.11 (темы) карточка обновляется вместе с именем темы, так что пару с карточками тоже спрашиваем снова,
    # когда одна из тем выросла вдвое: третье число — всегда 0
    stories = state["stories"]
    state.setdefault("checked", {})[f"{a}|{b}"] = [len(stories[a]["urls"]), len(stories[b]["urls"]), 0]


def _pair_answers(todo: list[tuple[str, str]], answers: dict[int, dict]) -> dict[int, str]:
    """Связь по каждой паре todo из ответов на пачки (номер пачки → ответ): номер пары → link."""
    links = {}
    for k, answer in answers.items():
        for p in answer["pairs"]:
            n = k * MERGE_BATCH + int(p["id"][1:]) if p["id"][1:].isdigit() else -1
            if 0 <= n < len(todo):
                links[n] = p["link"]
    return links


def merge_filter(state: dict, now: str, todo: list[tuple[str, str]], vectors: dict, ask) -> tuple[list, list]:
    """Пары склейки сначала — Haiku (MERGE_FILTER_MODEL), сразу. Пары, которые он назвал не связанными
    (MERGE_FILTER_DROP), — признаны разными, кроме каждой MERGE_CONTROL-й: её Sonnet видит для контроля.
    Возвращает (пары для Sonnet, что о каждой из них ответил Haiku: "" — не ответил). Склейку решает та же модель,
    что фильтрует (с 18.11), — фильтра нет: все пары сразу к ней."""
    if MERGE_FILTER_MODEL == MERGE_MODEL:
        return list(todo), [""] * len(todo)
    answers = {k: ask(MERGE_PROMPT, content, MERGE_SCHEMA, MERGE_EFFORT, MERGE_FILTER_MODEL)[0]
               for k, content in enumerate(merge_requests(state, todo, vectors))}
    links = _pair_answers(todo, answers)
    stats = _merge_stats(state, now)
    keep, haiku = [], []
    for n, (a, b) in enumerate(todo):
        link = links.get(n, "")
        if link in MERGE_FILTER_DROP and (int(a[1:]) + int(b[1:])) % MERGE_CONTROL:
            _mark_checked(state, a, b)
            stats["dropped"] = stats.get("dropped", 0) + 1
        else:
            keep.append((a, b))
            haiku.append(link)
    return keep, haiku


def _merge_stats(state: dict, now: str) -> dict:
    """Сводка склейки за день (UTC) — state["merge_stats"][день]: dropped — сколько пар отсеял Haiku,
    «haiku>sonnet» — сколько пар оба видели и как ответили (other>same — склейка, которую фильтр потерял бы)."""
    days = state.setdefault("merge_stats", {})
    stats = days.setdefault(now[:10], {})
    for d in sorted(days)[:-14]:
        del days[d]
    return stats


def merge_note(state: dict, now: str, todo: list[tuple[str, str]], haiku: list[str], answers: dict[int, dict]) -> None:
    """Ответы Sonnet на пары, которые видел и Haiku, — в сводку дня (_merge_stats)."""
    stats = _merge_stats(state, now)
    for n, link in _pair_answers(todo, answers).items():
        if n < len(haiku):
            key = f"{haiku[n] or '?'}>{link}"
            stats[key] = stats.get(key, 0) + 1


def is_big(state: dict, sid: str) -> bool:
    return len(state["stories"][sid]["urls"]) >= BIG


def whole(state: dict, vectors: dict, sid: str, count: int = WHOLE_SHOWN) -> list[str]:
    """Самые типичные новости темы за всё время: count из WHOLE_POOL, взятых равномерно, по времени."""
    urls = state["stories"][sid]["urls"]
    pool = [u for u in urls[::max(len(urls) // WHOLE_POOL, 1)] if u in vectors]
    if len(pool) > count:
        v = np.stack([vectors[u] for u in pool]).astype(np.float32)
        keep = set(np.argsort(-(v @ v.T).mean(axis=1))[:count])
        pool = [u for k, u in enumerate(pool) if k in keep]
    return list(dict.fromkeys(text_of(state["items"][u]) for u in pool))


def merge_event(state: dict, sid: str) -> str:
    """Ситуация темы для склейки — её карточка, кроме большой темы: её карточка — про первый эпизод (BIG)."""
    return "" if is_big(state, sid) else state.get("cards", {}).get(sid, "")


def merge_requests(state: dict, todo: list[tuple[str, str]], vectors: dict) -> list[str]:
    """Запросы склейки: пачки по MERGE_BATCH пар, k-я — пары todo[k * MERGE_BATCH:…]. Системный промпт — MERGE_PROMPT.
    У каждой темы — название, у большой вместо карточки — whole: склейка сравнивает темы целиком, а не первые эпизоды."""
    contents, names = [], state.get("names", {})
    for start in range(0, len(todo), MERGE_BATCH):
        chunk = todo[start:start + MERGE_BATCH]
        aliases = {}
        for a, b in chunk:
            for s in (a, b):
                aliases.setdefault(s, f"S{len(aliases)}")
        contents.append(json.dumps({"stories": [{"id": al}
                                                | ({"name": names[s]} if s in names else {})
                                                | ({"event": merge_event(state, s)} if merge_event(state, s) else {})
                                                | ({"whole": whole(state, vectors, s)} if is_big(state, s) else {})
                                                | {"news": _shown(state, s, vectors, MERGE_SHOWN_CARD if merge_event(state, s)
                                                                  else MERGE_SHOWN, detail=True)}
                                                for s, al in aliases.items()],
                                    "pairs": [{"id": f"P{n}", "a": aliases[a], "b": aliases[b]}
                                              for n, (a, b) in enumerate(chunk)]}, ensure_ascii=False))
    return contents


def merge_answer(state: dict, todo: list[tuple[str, str]], answer: dict, k: int) -> dict[int, tuple[str, str]]:
    """Ответ на k-ю пачку: номера пар в todo, чья связь — из KEEP_LINKS, → (поводы обоих сюжетов «a | b» — один,
    если совпали; связь). Повод сюжета с карточкой — карточка (кроме большой темы, merge_event), без неё — как его
    назвала модель. Повода нет —
    не склеиваем."""
    same = {}
    for p in answer["pairs"]:
        n = k * MERGE_BATCH + int(p["id"][1:]) if p["id"][1:].isdigit() else -1
        if p["link"] not in KEEP_LINKS or not 0 <= n < len(todo):
            continue
        a, b = ((merge_event(state, sid) if sid in state["stories"] else "") or event.strip()
                for sid, event in zip(todo[n], (p["event_a"], p["event_b"])))
        if a and b:
            same[n] = (a if a == b else f"{a} | {b}", p["link"])
    return same


def merge_apply(state: dict, now: str, todo: list[tuple[str, str]], same: dict[int, str], asked: set[int]) -> int:
    """Склейка по ответам модели: same — пары todo, признанные одной историей, asked — на какие пары модель ответила.
    Ответ мог прийти позже (пакетный режим): пары, чьих сюжетов уже нет или которые с тех пор разделили руками, —
    мимо. Каждая склейка — в журнал state["merges"] с частями, которые принёс склеенный сюжет: по нему unmerge её
    отменяет. Возвращает, сколько склеено."""
    stories = state["stories"]
    todo = [(a, b) if a in stories and b in stories and not _apart(state, a, b) else None for a, b in todo]
    shown = {s for pair in todo if pair for s in pair if visible(state, s)}
    # за проход сюжет склеивается не больше одного раза: цепочки a ← b ← c на первом прогоне стянули аварию,
    # границу и ДТП в один сюжет на 450 новостей. Остальные пары с ним проверим в следующий раз, уже со склеенным.
    # Невидимые обрывки цепочку не строят: видимый сюжет за проход принимает их сколько угодно
    used, merges = set(), []
    for n, pair in enumerate(todo):
        if pair is None:
            continue
        a, b = pair
        if n in same and a not in used and b not in used:
            if a in shown and b in shown:
                used.update((a, b))
                merges.append((a, b, same[n]))
            elif a in shown or b in shown:  # обрывок — в видимый, а не наоборот
                gone = a if a not in shown else b
                used.add(gone)
                merges.append((b if gone == a else a, gone, same[n]))
    for n, pair in enumerate(todo):
        if pair and n in asked and not {pair, pair[::-1]} & {m[:2] for m in merges} and not set(pair) & used:
            _mark_checked(state, *pair)

    moved = {}  # склеенный сюжет → куда ушёл: видимый мог принять обрывки и сам уйти в другой видимый
    for keep, gone, (event, link) in merges:
        while keep in moved:
            keep = moved[keep]
        moved[gone] = keep
        join(state, keep, gone, now, event, link)
    # склеенный сюжет вырос, но updated у него — время новостей, а не склейки: без пометки его пары, отложенные
    # из-за «не больше одного раза за проход», ждали бы новой новости
    grown = set()
    for keep in moved.values():
        while keep in moved:
            keep = moved[keep]
        grown.add(keep)
    state["merge_again"] = sorted(set(state.get("merge_again", [])) | grown)
    return len(merges)


def join(state: dict, keep: str, gone: str, now: str, event: str, link: str) -> None:
    """Тема gone — в тему keep, со всеми частями; запись в журнал склеек, по ней unmerge её отменит."""
    stories = state["stories"]
    # связь — в журнал и в историю: 16.11 из 16 склеек три соединили разные события (обыск в фонде PIO — с задержанием
    # декана, протест SMER — со словами Ракич, пробки после каникул — с возвращением в школу), а какую связь
    # модель назвала, видно не было
    state.setdefault("merges", []).append(
        {"t": now, "keep": keep, "gone": gone, "parts": sorted({state["items"][u]["part"] for u in stories[gone]["urls"]}),
         "event": event, "link": link})
    for u in stories[gone]["urls"]:
        it = state["items"][u]
        it["story"] = keep
        it.setdefault("history", []).append({"t": now, "why": "merge", "story": keep, "part": it["part"], "from": gone,
                                                "event": event, "link": link})
    stories[keep]["urls"] = sorted(stories[keep]["urls"] + stories[gone]["urls"], key=lambda u: state["items"][u]["seen"])
    stories[keep]["updated"] = max(stories[keep]["updated"], stories[gone]["updated"])
    state.setdefault("merged", {})[gone] = keep
    del stories[gone]


def merge(state: dict, now: str, vectors: dict, ask) -> dict:
    """Склейка дублей — сюжетов, на которые распалось одно событие (merge_todo), сразу: Haiku отсеивает несвязанные
    пары (merge_filter), решает Sonnet.
    Возвращает {"in", "out", "usd", "asked": {модель: пар}, "merged"}."""
    total = {"in": 0, "out": 0, "usd": 0.0, "asked": {}, "merged": 0}
    todo, haiku = merge_filter(state, now, merge_todo(state, now, vectors), vectors, ask)
    same, answers = {}, {}
    for k, content in enumerate(merge_requests(state, todo, vectors)):
        answers[k], usage = ask(MERGE_PROMPT, content, MERGE_SCHEMA, MERGE_EFFORT, MERGE_MODEL)
        for key in ("in", "out", "usd"):
            total[key] += usage.get(key, 0)
        same |= merge_answer(state, todo, answers[k], k)
    merge_note(state, now, todo, haiku, answers)
    total["asked"][MERGE_MODEL] = len(todo)
    total["merged"] = merge_apply(state, now, todo, same, set(range(len(todo))))
    return total


def lines_todo(state: dict, only: set[str] | None = None) -> list[str]:
    """Большие видимые сюжеты, которые пора проверить на отдельные события: в сюжете LINES_MIN новостей, а с прошлой
    проверки прибавилось LINES_STEP. only — проверять только эти (например, топ)."""
    if not LINES_ON:
        return []
    lines = state.get("lines", {})
    return [sid for sid, story in state["stories"].items()
            if (only is None or sid in only) and len(story["urls"]) >= LINES_MIN
            and len(story["urls"]) >= lines.get(sid, {}).get("size", 0) + LINES_STEP and visible(state, sid)]


def lines_request(state: dict, sid: str) -> str:
    """Запрос проверки сюжета на отдельные события. Системный промпт — LINES_PROMPT."""
    items = [state["items"][u] for u in state["stories"][sid]["urls"]]
    return json.dumps(({"event": state["cards"][sid]} if sid in state.get("cards", {}) else {})
                      | {"news": [{"n": it["n"], "t": it["seen"][5:16], "source": it["source"],
                                   "what": it.get("what") or it["title"]} for it in items]}, ensure_ascii=False)


def lines_apply(state: dict, now: str, sid: str, answer: dict, asked: int | None = None) -> list[dict]:
    """Ответ проверки — отдельные события в свои сюжеты (_spin_off); что нашли — в state["lines"][сюжет]: {"size", "t",
    "found"}. Модели не верим на слово: событие — не меньше LINES_NEWS новостей и LINES_OUTLETS изданий, и в сюжете
    остаётся не меньше LINES_NEWS. Ответ мог прийти позже (пакетный режим): номера, которых в сюжете уже нет, — мимо,
    а size — сколько новостей было в сюжете, когда его отдали модели (asked): пришедшие потом она не видела.
    Возвращает найденные события: {"name", "why", "news", "to"}."""
    story = state["stories"][sid]
    size = len(story["urls"])
    by_n = {state["items"][u]["n"]: u for u in story["urls"]}
    items = {n: state["items"][u] for n, u in by_n.items()}
    found, taken = [], set()
    for line in answer["lines"]:
        news = sorted({n for n in line["news"] if n in items} - taken)  # новость — только в одно событие
        if (len(news) >= LINES_NEWS and len({items[n]["source"] for n in news}) >= LINES_OUTLETS
                and size - len(taken) - len(news) >= LINES_NEWS and line["name"].strip()):
            taken |= set(news)
            found.append({"name": clean_name(line["name"]), "why": line["why"].strip(), "news": news})
    for line in found:
        line["to"] = _spin_off(state, sid, [by_n[n] for n in line["news"]], now)
    state.setdefault("lines", {})[sid] = {"size": asked or len(story["urls"]), "t": now, "found": found}
    return found


def find_lines(state: dict, now: str, ask, only: set[str] | None = None) -> dict:
    """Отдельные события внутри больших видимых сюжетов (lines_todo) — сразу: решает Sonnet.
    Возвращает {"in", "out", "usd", "checked", "found", "spun": [(сюжет, имя)]}."""
    total = {"in": 0, "out": 0, "usd": 0.0, "checked": 0, "found": 0, "spun": []}
    for sid in lines_todo(state, only):  # выделенные по ходу сюжеты не проверяем: их нет в списке
        answer, usage = ask(LINES_PROMPT, lines_request(state, sid), LINES_SCHEMA, LINES_EFFORT, MERGE_MODEL)
        for k in ("in", "out", "usd"):
            total[k] += usage.get(k, 0)
        found = lines_apply(state, now, sid, answer)
        total["spun"] += [(line["to"], line["name"]) for line in found]
        total["checked"] += 1
        total["found"] += len(found)
    return total


def clean_name(name: str) -> str:
    """Имя без кавычек вокруг и точки в конце. Кавычки названия внутри остаются: «Разрешение на «Югославию»»."""
    name = name.strip().rstrip(".").strip()
    while len(name) > 1 and name[0] + name[-1] in ("«»", '""', "''") and not set(name[0] + name[-1]) & set(name[1:-1]):
        name = name[1:-1].strip()
    return name


def _spin_off(state: dict, parent: str, urls: list[str], now: str) -> str:
    """Новости отдельного события — из сюжета parent в новый сюжет. Связь — state["spun"][новый] = parent; склейка
    их обратно не соединит (state["apart"]) — ни с ним, ни с событиями, выделенными из него раньше: 13.11 «Отвод
    прокурора» склеился с «Разрешением на «Югославию»», выделенным из того же сюжета. Возвращает новый сюжет."""
    sid = None
    for u in sorted(urls, key=lambda u: state["items"][u]["seen"]):
        sid = move(state, u, sid, now, part=sid, why="line")
    siblings = {p for c, was in state.get("spun", {}).items() if was == parent and c in state["stories"]
                for p in _parts(state, c)}
    state.setdefault("apart", []).append([[sid], sorted(_parts(state, parent) | siblings)])
    state.setdefault("spun", {})[sid] = parent
    return sid


def _parts(state: dict, sid: str) -> set[str]:
    return {state["items"][u]["part"] for u in state["stories"][sid]["urls"]}


def _apart(state: dict, a: str, b: str) -> bool:
    """Разделены ли сюжеты руками: в state["apart"] — пары списков частей, которые склейка не соединяет."""
    pa, pb = _parts(state, a), _parts(state, b)
    return any(pa & set(x) and pb & set(y) or pa & set(y) and pb & set(x) for x, y in state.get("apart", []))


def move(state: dict, url: str, sid: str | None, now: str, part: str | None = None, why: str = "move") -> str:
    """Перенос новости без модели: в сюжет sid или в новый (None). Часть part; по умолчанию в новом сюжете — своя
    (новое событие), в существующем — прежняя (событие новости не меняется). Опустевший сюжет удаляется.
    Возвращает сюжет."""
    it, stories = state["items"][url], state["stories"]
    old = it["story"]
    if old:
        stories[old]["urls"].remove(url)
        if not stories[old]["urls"]:
            del stories[old]
    if sid is None:
        sid = _start(state, it["seen"])
        part = part or sid
    elif sid not in stories:
        stories[sid] = {"created": it["seen"], "updated": it["seen"], "urls": []}
    story = stories[sid]
    story["urls"] = sorted(story["urls"] + [url], key=lambda u: state["items"][u]["seen"])
    story["created"] = min(story["created"], it["seen"])
    story["updated"] = max(story["updated"], it["seen"])
    it["story"], it["part"] = sid, part or it["part"]
    it.setdefault("history", []).append({"t": now, "why": why, "story": sid, "part": it["part"], "from": old})
    return sid


def split(state: dict, parts: list[str], now: str, sid: str | None = None, why: str = "split") -> str:
    """Части parts — в отдельный сюжет sid (по умолчанию — id первой части, если свободен). Склейка их с тем, откуда
    они ушли, обратно не соединит (state["apart"]). Возвращает сюжет."""
    urls = sorted((u for u, it in state["items"].items() if it["story"] and it["part"] in parts),
                  key=lambda u: state["items"][u]["seen"])
    if not urls:
        raise ValueError(f"нет новостей в частях {parts}")
    left = {state["items"][u]["story"] for u in urls}
    sid = sid or (parts[0] if parts[0] not in state["stories"] else _start(state, now))  # части не меняются
    for u in urls:
        sid = move(state, u, sid, now, why=why)
    for old in left:
        if old in state["stories"] and old != sid:
            state.setdefault("apart", []).append([sorted(set(parts)), sorted(_parts(state, old))])
    return sid


def unmerge(state: dict, gone: str, now: str) -> str:
    """Отмена склейки: части, которые принёс сюжет gone (последняя запись журнала), — снова сюжет gone."""
    record = next(m for m in reversed(state.get("merges", [])) if m["gone"] == gone)
    return split(state, record["parts"], now, gone if gone not in state["stories"] else None, why="unmerge")


def merge_due(state: dict, now: str) -> bool:
    last = state.get("merged_at")
    return not last or datetime.fromisoformat(now) - datetime.fromisoformat(last) >= MERGE_EVERY


def update(state: dict, items: list[tuple[str, dict]], now: str, vectors: dict, ask, later: bool = False) -> dict:
    """Раскладывает новости одной сборки: items — [(адрес, новость из архива)], now — время сборки (ISO),
    vectors — {адрес: вектор} для новых и для новостей живых сюжетов, ask(system, content, schema) → (ответ, токены).
    Новости, уже решённые раньше, и не-события пропускаются; сюжетам, ставшим видимыми, — карточки (cards).
    later — перепроверка пакетом: запросы копятся в state["recheck_queue"], ответы применяет recheck_apply.
    Возвращает {"in", "out", "decided"}."""
    todo = [(url, item) for url, item in items if url not in state["items"] and eligible(item)]
    total = {"in": 0, "out": 0, "decided": len(todo)}
    for start in range(0, len(todo), BATCH):
        usage = _decide(state, todo[start:start + BATCH], now, vectors, ask, later)
        total["in"] += usage["in"]
        total["out"] += usage["out"]
    cards(state, vectors, ask, now)  # сюжеты, ставшие видимыми в этой сборке: следующая сверяет новости уже с карточкой
    return total
