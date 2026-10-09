# -*- coding: utf-8 -*-
"""Render MLTDTranslationAssets architecture diagrams to PNG (Pillow).

Font strategy: msyh/msyhbd cover both CJK and Latin. Consolas is used only for
pure-ASCII strings that should look monospaced.
"""
import os
from PIL import Image, ImageDraw, ImageFont

F_CJK = r"C:\Windows\Fonts\msyh.ttc"
F_CJK_BD = r"C:\Windows\Fonts\msyhbd.ttc"
F_MONO = r"C:\Windows\Fonts\consola.ttf"
F_MONO_BD = r"C:\Windows\Fonts\consolab.ttf"
_CACHE = {}


def _font(path, size):
    k = (path, size)
    if k not in _CACHE:
        _CACHE[k] = ImageFont.truetype(path, size)
    return _CACHE[k]


def fT(size, bold=False, mono=False, text=""):
    """Pick a font that can actually draw `text`."""
    if mono and text and text.isascii():
        return _font(F_MONO_BD if bold else F_MONO, size)
    return _font(F_CJK_BD if bold else F_CJK, size)


# palette -------------------------------------------------------------------
BG = (247, 249, 252)
INK = (24, 32, 44)
MUTED = (94, 110, 132)
EDGE = (203, 213, 225)

C_EXT, E_EXT = (219, 234, 254), (59, 130, 246)
C_CI, E_CI = (254, 243, 205), (202, 138, 4)
C_SRC, E_SRC = (220, 252, 231), (22, 163, 74)
C_PIPE, E_PIPE = (237, 233, 254), (124, 58, 237)
C_GEN, E_GEN = (255, 228, 230), (225, 29, 72)
C_SRV, E_SRV = (207, 250, 254), (8, 145, 178)
C_OUT, E_OUT = (226, 232, 240), (71, 85, 105)
C_WARN, E_WARN = (254, 226, 226), (185, 28, 28)

ARROW = (71, 85, 105)


# primitives ----------------------------------------------------------------
def rrect(d, box, r, fill, edge, w=2):
    d.rounded_rectangle(box, radius=r, fill=fill, outline=edge, width=w)


def wrap(d, s, fnt, max_w):
    lines, cur = [], ""
    for ch in s:
        if ch == "\n":
            lines.append(cur)
            cur = ""
            continue
        if cur and d.textlength(cur + ch, font=fnt) > max_w:
            lines.append(cur)
            cur = ch
        else:
            cur += ch
    if cur or not lines:
        lines.append(cur)
    return lines


def fit_box(d, x, y, w, h, title, sub=None, fill=C_SRC, edge=E_SRC,
            mono_title=False, mono_sub=True, ts=24, ss=17, pad=12, min_ts=15):
    """Draw a node, shrinking fonts until the content fits."""
    rrect(d, (x, y, x + w, y + h), 11, fill, edge, 2)
    inner_w = w - 2 * pad
    inner_h = h - 2 * pad
    while ts > min_ts:
        tf = fT(ts, True, mono_title, title)
        tl = wrap(d, title, tf, inner_w)
        sf = fT(ss, False, mono_sub, sub or "")
        sl = wrap(d, sub, sf, inner_w) if sub else []
        need = len(tl) * (ts + 5) + (len(sl) * (ss + 4) + 5 if sl else 0)
        if need <= inner_h:
            break
        ts -= 1
        ss = max(12, ss - 1)
    ty = y + (h - need) / 2
    for ln in tl:
        d.text((x + w / 2, ty), ln, font=tf, fill=INK, anchor="ma")
        ty += ts + 5
    if sl:
        ty += 5
        for ln in sl:
            d.text((x + w / 2, ty), ln, font=sf, fill=MUTED, anchor="ma")
            ty += ss + 4


def v_arrow(d, x, y1, y2, color=ARROW, w=3, label=None, lf_size=18, lx=None,
            ly=None, dotted=False):
    if dotted:
        step, cur = 13, y1
        while cur < y2 - 12:
            d.line((x, cur, x, min(cur + 7, y2 - 12)), fill=color, width=w)
            cur += step
    else:
        d.line((x, y1, x, y2 - 12), fill=color, width=w)
    d.polygon([(x, y2), (x - 9, y2 - 13), (x + 9, y2 - 13)], fill=color)
    if label:
        f = fT(lf_size, True, False, label)
        tw = d.textlength(label, font=f)
        bx = (lx if lx is not None else x + 12)
        by = (ly if ly is not None else (y1 + y2) / 2)
        d.rectangle((bx - 7, by - 15, bx + tw + 7, by + 15), fill=BG)
        d.text((bx, by), label, font=f, fill=color, anchor="lm")


def h_arrow(d, x1, x2, y, color=ARROW, w=3, label=None, lf_size=18):
    d.line((x1, y, x2 - 12, y), fill=color, width=w)
    d.polygon([(x2, y), (x2 - 13, y - 9), (x2 - 13, y + 9)], fill=color)
    if label:
        f = fT(lf_size, True, False, label)
        tw = d.textlength(label, font=f)
        cx = (x1 + x2) / 2
        d.rectangle((cx - tw / 2 - 7, y - 30, cx + tw / 2 + 7, y - 4), fill=BG)
        d.text((cx, y - 17), label, font=f, fill=color, anchor="mm")


def elbow(d, pts, color=ARROW, w=3, label=None, lf_size=18, lpos=None):
    for i in range(len(pts) - 2):
        d.line((pts[i][0], pts[i][1], pts[i + 1][0], pts[i + 1][1]), fill=color,
               width=w)
    (x1, y1), (x2, y2) = pts[-2], pts[-1]
    if y1 == y2:
        d.line((x1, y1, x2 - 12, y2), fill=color, width=w)
        d.polygon([(x2, y2), (x2 - 13, y2 - 9), (x2 - 13, y2 + 9)], fill=color)
    else:
        d.line((x1, y1, x2, y2 - 12), fill=color, width=w)
        d.polygon([(x2, y2), (x2 - 9, y2 - 13), (x2 + 9, y2 - 13)], fill=color)
    if label:
        f = fT(lf_size, True, False, label)
        tw = d.textlength(label, font=f)
        lx, ly = lpos or pts[0]
        d.rectangle((lx - 7, ly - 15, lx + tw + 7, ly + 15), fill=BG)
        d.text((lx, ly), label, font=f, fill=color, anchor="lm")


def title(d, x, y, main, sub):
    d.text((x, y), main, font=_font(F_CJK_BD, 34), fill=INK)
    d.text((x, y + 46), sub, font=_font(F_CJK, 19), fill=MUTED)


# ============================================================================
# Diagram A : layered component architecture
# ============================================================================
def diagram_layers(path):
    W = 2360
    M = 36
    LW = W - 2 * M
    lanes = [
        ("① 上游系统 / External", "公网只读来源与外部状态；本仓只读不写", 210, C_EXT, E_EXT),
        ("② CI 编排 / GitHub Actions", "4 个 workflow —— 仓库内唯一的自动化写入者", 320, C_CI, E_CI),
        ("③ 源数据 / Source of truth", "git 跟踪，PR 审核入口；行级 status 状态机", 260, C_SRC, E_SRC),
        ("④ 离线流水线 / pipelines + research-tools", "本地手工执行，不跑在 CI runner 上", 250, C_PIPE, E_PIPE),
        ("⑤ 生成物 / generated", "内容寻址存储，CI 事务式单写者发布", 270, C_GEN, E_GEN),
        ("⑥ 分发与运行 / asset-server + server + tools", "候选闭包，未部署；只读路由逐请求复验", 300, C_SRV, E_SRV),
        ("⑦ 消费方 / Consumers", "客户端 / 门户 / 镜像读者", 150, C_OUT, E_OUT),
    ]
    GAP = 66
    top = 108
    ys, y = [], top
    for _, _, h, _, _ in lanes:
        ys.append(y)
        y += h + GAP
    H = y - GAP + 44

    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    title(d, M, 26, "MLTDTranslationAssets — 分层组件架构",
          "实线箭头 = 依赖/数据流；② 是唯一自动 writer，⑤ 是唯一内容寻址真源，⑥ 尚未部署")

    for (t, s, h, fill, edge), y0 in zip(lanes, ys):
        rrect(d, (M, y0, M + LW, y0 + h), 16, fill, edge, 2)
        d.rectangle((M, y0, M + 10, y0 + h), fill=edge)
        d.text((M + 28, y0 + 18), t, font=_font(F_CJK_BD, 25), fill=INK)
        d.text((M + 28 + d.textlength(t, font=_font(F_CJK_BD, 25)) + 26, y0 + 25),
               s, font=_font(F_CJK, 19), fill=MUTED)

    def row(items, y0, h, lane_h, pad_top=64, gap=20, mono=True, ts=24, ss=16,
            pad=12):
        avail = LW - 60
        weights = [it[2] for it in items]
        tot = sum(weights)
        gaps = gap * (len(items) - 1)
        x = M + 30
        for (t2, s2, _), wt in zip(items, weights):
            w = (avail - gaps) * wt / tot
            fit_box(d, x, y0 + pad_top, w, lane_h - pad_top - 16, t2, s2,
                    it_fill[0], it_edge[0], mono_title=mono, mono_sub=mono,
                    ts=ts, ss=ss, pad=pad)
            x += w + gap

    # lane 1
    y0 = ys[0]
    it_fill, it_edge = (C_EXT,), (E_EXT,)
    row([("Matsurihi API", "api.matsurihi.me", 34),
         ("官方 assets CDN", "td-assets.bn765.com\n/{ver}/production/2018/Android", 40),
         ("LLM Provider Gateway", "cpa.nyaneko.cn/v1\nprotocol = responses", 34)],
        y0, 0, lanes[0][2], 64, 24, True, 25, 17, 12)

    # lane 2
    y0 = ys[1]
    it_fill, it_edge = (C_CI,), (E_CI,)
    hh = lanes[1][2]
    rowh = 100
    pr = 62
    def ci_row(items, ytop):
        avail = LW - 60
        weights = [i[2] for i in items]
        tot = sum(weights)
        gaps = 24 * (len(items) - 1)
        x = M + 30
        for (t2, s2, _), wt in zip(items, weights):
            w = (avail - gaps) * wt / tot
            fit_box(d, x, ytop, w, rowh, t2, s2, C_CI, E_CI, mono_title=True,
                    mono_sub=True, ts=22, ss=16, pad=10)
            x += w + 24
    ci_row([("auto-track-jp-assets.yml", "cron 0 16 * * *  ·  冻结 tag assets-<ver>\n→ 只开 PR，绝不自动合并", 60),
            ("llm-translate-assets.yml", "cron 17 2 * * *  ·  官方增量 → LLM → 直接提交 main", 62)], y0 + pr)
    ci_row([("validate-localization.yml", "PR / push  ·  promote 预演 + validate_repo.py", 56),
            ("assets-generated.yml", "push main  ·  promote → build → verify → portal manifest\n→ 提交带 [skip ci]，不自触发", 76)], y0 + pr + rowh + 20)

    # lane 3
    y0 = ys[2]
    it_fill, it_edge = (C_SRC,), (E_SRC,)
    row([("locales/", "11,818 个 JSONL · 391,618 行\nmaster 2482 / story 3657 / card 2997\nbirth 1605 / dialogue 1077", 40),
         ("lyrics/", "432 曲分轨 · 12,131 槽\nall_lyrics.jsonl", 30),
         ("glossary/", "authoritative-terms\nidols.json（52 偶像）", 30),
         ("images/localized/", "937 张已审 PNG\nSHA-256 索引", 30),
         ("manifests/", "asset-version · bundle-index\nimages · portal-resource", 38),
         ("schema/", "entry.schema.json\nassets-generated-\nmanifest.schema.json", 30)],
        y0, 0, lanes[2][2], 64, 18, True, 25, 16, 10)

    # lane 4
    y0 = ys[3]
    it_fill, it_edge = (C_PIPE,), (E_PIPE,)
    row([("pipelines/text/", "GTX 解密/回写 · PromptCompiler\n翻译池 · QA · overlay 合流", 38),
         ("pipelines/image/", "Sprite Atlas 重组 · 图像重绘\n视觉审校 · ASTC 无损回填", 38),
         ("pipelines/export/", "export_localization\n_for_github.py", 28),
         ("research-tools/", "迁移候选：materialize_\ngenerated_release.py（未接入）", 36),
         ("scripts/（19 个文件）", "assets_generated_index · build_generated_release\nassets_mirror · promote · validate · track_jp_assets", 48)],
        y0, 0, lanes[3][2], 64, 18, True, 24, 16, 12)

    # lane 5
    y0 = ys[4]
    it_fill, it_edge = (C_GEN,), (E_GEN,)
    row([("generated/<asset_version>/", "manifest.json  13.5 MB\nchecksums.txt  1.7 MB\n1077100 · 1077600\n1077640 · 1077650\n1077700", 40),
         ("generated/objects/sha256/<sha256>", "11,844 个 CAS 对象 · 274.8 MB\nflat 布局 · 跨版本去重\n旧 sharded 布局仅读兼容", 44),
         ("manifest entry 字段", "channel · asset_version · client_version(null)\nsource/translation/generated_commit · ci_run_id\nlogical_key · logical_path · runtime_path\nsource_sha256 · object_path · artifact_sha256\nreuse_status · translation_status · resource_kind", 56)],
        y0, 0, lanes[4][2], 64, 24, True, 25, 16, 14)

    # lane 6
    y0 = ys[5]
    it_fill, it_edge = (C_SRV,), (E_SRV,)
    row([("scripts/assets_mirror.py", "sync / watch / activate / retain\nprune / verify / resolve / list\n同步但不激活、不清理", 42),
         ("mirror root（NAS/卷）", "objects/sha256/<digest>\npublished/<ver>/{manifest,\nchecksums,state}", 40),
         ("asset-server/assets_route.py", "只读 HTTP（loopback:8765）\nGET/HEAD /assets/<ver|current>/…\n逐请求复验字节 · ETag\nPOST/PUT/DELETE → 405", 46),
         ("server/ + tools/", "versioned_asset_store\nasset_archive\narchive_controller\nasset_version CLI", 34)],
        y0, 0, lanes[5][2], 64, 24, True, 25, 16, 12)

    # lane 7
    y0 = ys[6]
    it_fill, it_edge = (C_OUT,), (E_OUT,)
    row([("游戏客户端", "优先取 /generated-assets/<ver>/…\n缺失时回源官方 JP", 40),
         ("社区门户与协作者", "审校 / 反馈（静态 Pages）", 32),
         ("镜像节点", "只读消费 published/<ver>", 28),
         ("维护者", "PR · 离线流水线 · 人工复核", 32)],
        y0, 0, lanes[6][2], 60, 24, True, 24, 17, 12)

    # inter-lane arrows
    labels = ["版本探测 / 官方 bundle 增量 / LLM 调用",
              "CI 读写仓库文件（唯一自动 writer）",
              "本地手工调用（离线）",
              "正式生成（事务式单写者）",
              "sync / watch（候选，未部署）",
              "HTTP GET（只读分发）"]
    for i in range(len(lanes) - 1):
        a = ys[i] + lanes[i][2]
        b = ys[i + 1]
        v_arrow(d, M + 760, a + 6, b - 6, ARROW, 4, labels[i], 19, M + 780)

    img.save(path)
    print("saved", path, img.size)


# ============================================================================
# Diagram B : end-to-end release flow
# ============================================================================
def diagram_flow(path):
    W = 2860
    M = 36
    lanes = [
        ("① 触发 / Triggers", 130, 150, (241, 245, 249), (148, 163, 184)),
        ("② CI 工作流 / GitHub Actions", 350, 230, (254, 252, 232), (202, 138, 4)),
        ("③ 仓库产物 / git main", 650, 220, (240, 253, 244), (22, 163, 74)),
        ("④ 外部状态 / 官方与 LLM", 940, 200, (239, 246, 255), (59, 130, 246)),
        ("⑤ 消费 / Consumers", 1210, 120, (248, 250, 252), (100, 116, 139)),
    ]
    H = 1440
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    title(d, M, 26, "MLTDTranslationAssets — 端到端数据流",
          "编号 = 时序；青色 = 自动链路，橙色 = 人工/离线链路。写 main 的动作都带门禁、冲突重试与 [skip ci] 抑制")

    lx = M + 268
    lw = W - M - lx - 24
    for t, y0, h, fill, edge in lanes:
        rrect(d, (M, y0, W - M, y0 + h), 14, fill, edge, 2)
        d.text((M + 18, y0 + 13), t, font=_font(F_CJK_BD, 22), fill=INK)

    cols = 4
    gap = 30
    cw = (lw - gap * (cols - 1)) / cols
    xs = [lx + i * (cw + gap) for i in range(cols)]

    def cell(i, y0, h, t, s, fill, edge, mono=True, ts=20, ss=15):
        fit_box(d, xs[i], y0, cw, h, t, s, fill, edge, mono_title=mono,
                mono_sub=mono, ts=ts, ss=ss, pad=10)

    # --- lane 1 triggers
    ty = 136
    cell(0, ty, 118, "① PR 合入 main", "协作者 / 门户\n（人工审核入口）", C_SRC, E_SRC, False, 21, 16)
    cell(1, ty, 118, "② cron 02:17 UTC", "LLM 批量翻译", C_CI, E_CI, False, 21, 16)
    cell(2, ty, 118, "③ cron 16:00 UTC", "跟踪日版新资源版本", C_CI, E_CI, False, 21, 16)
    cell(3, ty, 118, "④ push main", "locales/ manifests/ 变更", C_CI, E_CI, False, 21, 16)

    # --- lane 2 CI
    cy = 388
    cell(0, cy, 160, "validate-localization.yml", "promote_merged_locales.py --before/--after（预演）\n+ scripts/validate_repo.py", C_CI, E_CI)
    cell(1, cy, 160, "llm-translate-assets.yml", "track_jp_assets → refresh_latest_official_catalogue\n→ collect → translate_mltd_api_pool → apply\n→ validate_repo → 提交 main", C_CI, E_CI)
    cell(2, cy, 160, "auto-track-jp-assets.yml", "track_jp_assets --check-only（exit 10 = 有更新）\n→ freeze（tag assets-<ver>，不可移动）\n→ 只开 PR，不合并", C_CI, E_CI)
    cell(3, cy, 160, "assets-generated.yml", "promote → validate_repo → build_generated_release\n→ assets_generated_index verify\n→ build_portal_resource_manifest → 提交 [skip ci]", C_CI, E_CI)

    # --- lane 3 repo
    ry = 690
    cell(0, ry, 140, "locales/**/*.jsonl", "391,618 行 · 5 域\nstatus: untranslated→pending→accepted\ntranslation_stage 生命周期标记", C_SRC, E_SRC)
    cell(1, ry, 140, "locales/ + manifests/", "official-bundle-index.json（增量 memo）\nasset-version.json", C_SRC, E_SRC)
    cell(2, ry, 140, "manifests/asset-version.json", "asset_version = 1077710\n+ git tag assets-<ver>", C_SRC, E_SRC)
    cell(3, ry, 140, "generated/", "generated/<ver>/manifest.json + checksums.txt\ngenerated/objects/sha256/<digest>（CAS）\n+ manifests/portal-resource-manifest.json", C_GEN, E_GEN)

    # --- lane 4 external
    ey = 978
    cell(0, ey, 124, "Matsurihi API", "api.matsurihi.me\n版本探测", C_EXT, E_EXT, False, 20, 15)
    cell(1, ey, 124, "LLM Gateway", "cpa.nyaneko.cn/v1\nkey 仅来自 Actions secret", C_EXT, E_EXT, False, 20, 15)
    cell(2, ey, 124, "官方 assets CDN", "td-assets.bn765.com\n只读基线，绝不回写", C_EXT, E_EXT, False, 20, 15)
    cell(3, ey, 124, "GitHub API / git", "commit · tag · PR\napi.github.com", C_EXT, E_EXT, False, 20, 15)

    # --- lane 5 consumers
    oy = 1244
    cell(0, oy, 66, "游戏客户端", "/assets/<ver>/… 优先", C_OUT, E_OUT, False, 20, 15)
    cell(1, oy, 66, "社区门户 / 协作者", "静态站审校 · 反馈", C_OUT, E_OUT, False, 20, 15)
    cell(2, oy, 66, "客户端回源", "缺失时取官方 JP", C_OUT, E_OUT, False, 20, 15)
    cell(3, oy, 66, "镜像节点 / NAS", "只读消费 published/<ver>", C_OUT, E_OUT, False, 20, 15)

    cx = [x + cw / 2 for x in xs]
    # triggers -> CI
    for i in range(cols):
        v_arrow(d, cx[i], ty + 118 + 4, cy - 4, ARROW, 3, None)
        d.text((cx[i] - 16, ty + 118 + 20), str(i + 1), font=_font(F_CJK_BD, 22),
               fill=ARROW, anchor="ra")
    # CI -> repo
    for i in range(cols):
        v_arrow(d, cx[i], cy + 160 + 4, ry - 4, ARROW, 3, None)
        d.text((cx[i] - 16, cy + 160 + 20), str(i + 5), font=_font(F_CJK_BD, 22),
               fill=ARROW, anchor="ra")
    # external -> CI : read-only inputs, routed through the gutters below the repo lane
    elbow(d, [(cx[0], ey), (cx[0], ey - 30), (xs[2] - 14, ey - 30), (xs[2] - 14, cy + 160 + 4)],
          (2, 132, 199), 3, "9 版本探测", 18, (cx[0] + 12, ey - 30))
    elbow(d, [(cx[1], ey), (cx[1], ey - 64), (xs[1] - 14, ey - 64), (xs[1] - 14, cy + 160 + 4)],
          (2, 132, 199), 3, "10 LLM 调用（key 仅取自 secret）", 18, (cx[1] + 12, ey - 64))
    elbow(d, [(cx[2], ey), (cx[2], ey - 98), (xs[3] - 14, ey - 98), (xs[3] - 14, cy + 160 + 4)],
          (2, 132, 199), 3, "11 官方 bundle 只读输入", 18, (cx[2] + 12, ey - 98))
    # repo -> consumers (serving layer is candidate / not deployed)
    # The last column now sits at the canvas edge, so right-align this label
    # against the arrow instead of letting it run off the bitmap.
    mirror_label = "12 assets_mirror → assets_route（候选，未部署）"
    mirror_w = d.textlength(mirror_label, font=fT(18, True, False, mirror_label))
    elbow(d, [(cx[3], ry + 140), (cx[3], oy - 4)],
          (8, 145, 178), 3, mirror_label, 18, (cx[3] - mirror_w - 40, 850))
    d.text((M + 18, H - 40),
           "门禁要点：promote 只晋级 diff 内 status∈{pending,untranslated} 且 zh 非空的行；validate_repo 先于 build；"
           "build 失败不创建/不覆盖 generated/<ver>/；verify 复验每个对象；push main 三次 rebase 重试、绝不 force。",
           font=_font(F_CJK, 18), fill=MUTED)

    img.save(path)
    print("saved", path, img.size)


# ============================================================================
# Diagram C : automation capability matrix
# ============================================================================
def diagram_automation(path):
    W, H = 2300, 1640
    M = 36
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    title(d, M, 26, "MLTDTranslationAssets — 新资源到达后的自动化能力矩阵",
          "绿 = CI 全自动；橙 = 门户/人工触发后自动；红 = 无自动化，必须离线人工操作")

    BADGE = {
        "auto": ((209, 250, 229), (4, 120, 87), "自动"),
        "semi": ((254, 243, 199), (180, 83, 9), "人工触发"),
        "none": ((254, 226, 226), (185, 28, 28), "无自动化"),
    }

    gap = 40
    cw = (W - 2 * M - 2 * gap) / 3
    cx = [M + i * (cw + gap) for i in range(3)]
    heads = [
        ("A. 文本面 / GTX TextAsset", "7 步全自动 · 1 步人工语义把关", C_SRC, E_SRC),
        ("B. 图像面 / Texture2D · Sprite", "5 步全无自动化 · 图片只走离线人工", C_WARN, E_WARN),
        ("C. 版本与发布面", "5 步全自动 · 分发端尚未部署", C_CI, E_CI),
    ]
    hy, hh = 118, 66
    for (t, s, fill, edge), x in zip(heads, cx):
        rrect(d, (x, hy, x + cw, hy + hh), 12, fill, edge, 2)
        d.text((x + 18, hy + 10), t, font=_font(F_CJK_BD, 23), fill=INK)
        d.text((x + 18, hy + 39), s, font=_font(F_CJK, 17), fill=MUTED)

    steps = [
        [  # A
            ("Matsurihi 探测新 asset_version", "track_jp_assets.py --check-only  → exit 10", "auto"),
            ("增量下载「已跟踪」bundle", "official-bundle-index.json CAS memo\nremote 未变则跳过", "auto"),
            ("抽取 GTX 源文本", "mltd_localization_pipeline.py extract-snapshot\n（只 read_gtx）", "auto"),
            ("追加 untranslated 行", "locales/master/official-<ver>-untranslated.jsonl\ncap 5000 行，超限 fail-closed", "auto"),
            ("LLM 翻译池", "translate_mltd_api_pool.py --batch-mode single", "auto"),
            ("写回 pending / llm_translated", "llm_translate_untranslated.py apply\n永不写 accepted，未变更行逐字节保留", "auto"),
            ("人工审校 → accepted", "PR 审核；这是唯一的语义把关点", "semi"),
            ("构建 Unity3D", "build_generated_release.py（只读 accepted 行）", "auto"),
        ],
        [  # B
            ("发现新纹理 / 新图集", "无任何 CI 或脚本枚举新 texture", "none"),
            ("Sprite Atlas 重组 + 重绘", "pipelines/image/（离线，gpt-image-2.5-sunburst）", "none"),
            ("视觉审校", "gpt-5.6-luna + 人工（离线）", "none"),
            ("（原）门户上传 → CI 回填", "backfill-image.yml：D1 队列 + R2 上传\n2026-10-09 随 Cloudflare 下线删除", "none"),
            ("进入 generated 发布", "— 未接入：--require-images 无条件拒绝", "none"),
        ],
        [  # C
            ("冻结 tag assets-<ver>", "不可移动；已存在即失败", "auto"),
            ("PR 预演 + 全仓校验", "promote_merged_locales.py + validate_repo.py", "auto"),
            ("状态晋级", "只会晋级 diff 内 pending/untranslated 且 zh 非空的行", "auto"),
            ("build → verify", "assets_generated_index.py verify 复验每个对象", "auto"),
            ("提交 main", "带 [skip ci]，不自触发", "auto"),
            ("镜像 + 只读分发", "assets_mirror.py → assets_route.py（候选，未部署）", "semi"),
        ],
    ]

    sy, sh, sgap = 210, 96, 18
    for i, col in enumerate(steps):
        x = cx[i]
        y = sy
        for j, (t, s, kind) in enumerate(col):
            bfill, bedge, blabel = BADGE[kind]
            fit_box(d, x, y, cw, sh, t, s, (255, 255, 255), bedge, mono_title=False,
                    mono_sub=True, ts=21, ss=15, pad=12)
            # badge
            bf = _font(F_CJK_BD, 16)
            tw = d.textlength(blabel, font=bf)
            bx2 = x + cw - 12
            bx1 = bx2 - tw - 20
            d.rounded_rectangle((bx1, y + 9, bx2, y + 35), radius=8, fill=bfill,
                                outline=bedge, width=2)
            d.text(((bx1 + bx2) / 2, y + 22), blabel, font=bf, fill=bedge, anchor="mm")
            if j < len(col) - 1:
                v_arrow(d, x + cw / 2, y + sh, y + sh + sgap, (148, 163, 184), 3)
            y += sh + sgap
    bottom = sy + 8 * (sh + sgap)

    # warning banner
    wy = bottom + 6
    rrect(d, (M, wy, W - M, wy + 168), 14, (255, 247, 237), (234, 88, 12), 3)
    d.text((M + 24, wy + 18), "两个必须知道的断点", font=_font(F_CJK_BD, 26), fill=(180, 83, 9))
    lines = [
        "1) 全新 bundle 不会被自动发现。scripts/refresh_latest_official_catalogue.py 先构造 known_bundles（来自 locales/**/*.jsonl 已有的 bundle 字段），",
        "    再 selected = {logical: row for logical, row in index.items() if logical.casefold() in known_bundles} —— 官方 index 里从没在 locales/ 出现过的 bundle",
        "    既不下载、也不抽取、也不会写进 memo。实测：official-bundle-index.json 11,816 条 = locales 的 distinct bundle 11,816 条，两集合完全相同。",
        "2) 图像面完全不在自动链路里。build_generated_release.py 的 --require-images 无条件 raise（源码明言「无图像物化/注入步骤，也无独立图像审计」），",
        "    只在 manifest 里写 image_surface: blocked_missing_reviewed_inputs 作为状态标记。CI 里已无任何图像步骤（原 backfill-image.yml 只是门户驱动回填，2026-10-09 已删除）。",
    ]
    ty = wy + 58
    for ln in lines:
        d.text((M + 28, ty), ln, font=_font(F_CJK, 18), fill=(124, 45, 18))
        ty += 22

    # evidence strip
    ey2 = wy + 190
    rrect(d, (M, ey2, W - M, ey2 + 176), 14, (241, 245, 249), EDGE, 2)
    d.text((M + 24, ey2 + 16), "实测痕迹（仓库内真实产物）", font=_font(F_CJK_BD, 24), fill=INK)
    ev = [
        "locales/master/official-1077710-untranslated.jsonl   1,833 行 · 5 个 bundle（MD_jp / CD_jp / MB_jp / CM_jp / ST_jp）· 全部为既有 bundle",
        "   → status: pending 1,830 + untranslated 3   ·   translation_stage: llm_translated 1,830   ·   zh 非空 1,830",
        "locales/master/official-1077640-untranslated.jsonl   4 行（birth_bdl2_001har_005_jp.gtx）· 全部 pending",
        "即：自动链路确实端到端跑通，但作用面是「已跟踪 bundle 的增量新文本」，不是「新资源面的发现」。",
    ]
    ty = ey2 + 54
    for i, ln in enumerate(ev):
        f = _font(F_CJK_BD if i == 3 else F_CJK, 18)
        d.text((M + 28, ty), ln, font=f, fill=INK if i == 3 else MUTED)
        ty += 26

    img.save(path)
    print("saved", path, img.size)


if __name__ == "__main__":
    out = os.path.dirname(os.path.abspath(__file__))
    diagram_layers(os.path.join(out, "layers.png"))
    diagram_flow(os.path.join(out, "flow.png"))
    diagram_automation(os.path.join(out, "automation.png"))

