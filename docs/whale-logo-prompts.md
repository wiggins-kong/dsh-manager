# DSH-manager 鲸鱼圆角 Logo — 生成提示词方案

> 项目背景：DSH-manager 管理 DeepSeek Harness 的完整生命周期，DeepSeek 品牌意象即鲸鱼。
> 生成要求：顶级图像模型（优先 GPT Image 2，备选 Seedance 5.0 Pro / Nano Banana Pro / Nano Banana 2）。
> 每张候选独立生成一次，**不要**让模型拼 contact sheet / 网格。
> 生成尺寸：请求 1536×1536，1:1 方形；若服务上限为 1254×1254 则直接接受。
> 生成后用本目录同级的 `assets/make_rounded.py`（PIL）把外角裁成圆角，导出 exe/界面用图标。

## 约束交付模式

- GPT Image 2 / Nano Banana / Seedance 5.0 Pro：使用完整主提示词，排除项写在 `Constraints:` 行（见下方各候选）。
- 若所用运行时暴露独立 `negative_prompt` 参数：主提示词删去 `Constraints:` 行，负向 payload 固定为：
  `text, watermark, borders, frames, cards, presentation masks, extra subjects, scenery, thin fragile lines, sharp tips, photorealistic materials, strong three-dimensional rendering, external cast shadows`

## 色彩与角位分配

| 标签 | 方向 | 入画角 | IP 色 1（主体） | IP 色 2（辅助） | 背景色（柔和低饱和） |
|------|------|--------|----------------|----------------|--------------------|
| A1 | 圆滚滚蓝鲸宝宝 | 左下 | 深海蓝 `#2F6FB5` | 浅雾白 `#EAF2F8`（肚皮/面部） | 柔和珊瑚杏 `#F3D9C6` |
| A2 | 圆滚滚蓝鲸宝宝 | 右下 | 深海蓝 `#2F6FB5` | 浅雾白 `#EAF2F8` | 柔和珊瑚杏 `#F3D9C6` |
| B1 | 鲸鱼+代码容器 | 左下 | 深海蓝 `#2F6FB5` | 暖奶黄 `#F6D77B`（背上方块） | 灰调薄荷绿 `#D8E8DE` |
| B2 | 鲸鱼+代码容器 | 右下 | 深海蓝 `#2F6FB5` | 暖奶黄 `#F6D77B` | 灰调薄荷绿 `#D8E8DE` |
| C1 | 眨眼笑脸鲸 | 左下 | 靛蓝紫 `#5B5FC7`（呼应 DeepSeek 主色） | 浅雾白 `#EAF2F8`（肚皮/面部） | 柔和奶油米 `#F2EAD9` |
| C2 | 眨眼笑脸鲸 | 右下 | 靛蓝紫 `#5B5FC7` | 浅雾白 `#EAF2F8` | 柔和奶油米 `#F2EAD9` |

---

## 通用骨架（每条候选把 `<...>` 替换后使用）

```text
Create one complete full-bleed 1:1 square image.
Background: fill the entire square with solid <背景色>. Keep <背景色> visible in every open area and in the corners not occupied by the character; the assigned emergence corner must be occupied by the character.
Subject: place one extremely simplified, cute, endearing <主体描述> IP character on the background, reduced to one soft rounded continuous silhouette and one defining feature.
Complexity: use only 4–7 large basic shapes and at most two broad internal color regions. Use two simple eyes and add one tiny mouth only when it helps the expression. Remove every nonessential line, outline, anatomical detail, texture, and decoration. Keep the character readable at 32 × 32.
Color behavior: use exactly three semantic colors in the complete image: exactly two IP base colors plus the background color. Choose the two IP colors from the subject and context, organize both into broad purposeful masses, and reuse them for facial marks. Keep the IP, facial marks, and background clearly separated.
Composition: keep the character upright and emerging from the assigned <入画角>, filling about 85–95% of the square so it remains visually dominant. Cropping at the bottom or assigned side is welcome when it strengthens the corner emergence. Preserve both paired identifying features. Never center or bottom-center the character.
Style: make simplification, cuteness, and lovable baby-like appeal the strongest qualities. Use large soft forms, compact proportions, thick rounded contours, and an ultra-clean graphic treatment. Prefer one clear shape over several explanatory details. Add an extremely, extremely subtle, almost imperceptible sense of depth through a barely-there neo-skeuomorphic treatment.
Finish: show only the character on the full-canvas background, with clean surfaces and normal square outer corners.
Constraints: Use no text or watermark. Add no borders, frames, cards, or presentation masks. Include one character only, with no extra subjects or scenery. Use no fragile lines, sharp tips, unnecessary outlines, tiny details, or decorative marks. Add no photorealistic material, dramatic bevel, glossy hotspot, deep occlusion, extrusion, strong three-dimensional rendering, or external cast shadow. Keep the background solid and uniform, with no texture, vignette, or lighting variation.
```

---

## A1 — 圆滚滚蓝鲸宝宝 · 左下

主体描述：baby blue whale — one huge rounded teardrop body with an oversized head, a tiny blunt rounded tail fin and a small rounded flipper on the visible side; a broad pale belly patch as the second color region; a tiny rounded water spout bump on top of the head; widely spaced simple dot eyes and a tiny calm smile.
入画角：lower-left。色：主体深海蓝 #2F6FB5，肚皮/面部浅雾白 #EAF2F8，背景柔和珊瑚杏 #F3D9C6。

## A2 — 圆滚滚蓝鲸宝宝 · 右下

与 A1 完全相同的主体描述与配色，仅入画角改为：lower-right。

## B1 — 鲸鱼+代码容器 · 左下

主体描述：baby whale carrying one plump rounded square container strapped on its back like a small backpack; the whale is one huge rounded continuous silhouette with a blunt rounded tail fin and small rounded flipper; the rounded square on its back is the second color region and sits clearly visible above the whale's back; widely spaced simple dot eyes and a tiny calm smile.
入画角：lower-left。色：鲸体深海蓝 #2F6FB5，背上方块暖奶黄 #F6D77B，背景灰调薄荷绿 #D8E8DE。方块上不画任何按钮、屏幕内容或符号（仅一块纯色圆润方块）。

## B2 — 鲸鱼+代码容器 · 右下

与 B1 完全相同的主体描述与配色，仅入画角改为：lower-right。

## C1 — 眨眼笑脸鲸 · 左下

主体描述：an extreme close-up style cute whale head-and-body mark — the face fills most of the character: one enormous rounded head mass, a tiny blunt rounded tail fin peeking from the far edge, a broad pale face/belly patch, very widely spaced large simple dot eyes and one tiny rounded smile as the only expression marks.
入画角：lower-left。色：主体靛蓝紫 #5B5FC7，面部/肚皮浅雾白 #EAF2F8，背景柔和奶油米 #F2EAD9。

## C2 — 眨眼笑脸鲸 · 右下

与 C1 完全相同的主体描述与配色，仅入画角改为：lower-right。

---

## 交付与后处理

1. 每个候选只生成一次，原样保留，不做合规过滤、不重试、不修图。
2. 命名：`assets/whale/A1.png` … `assets/whale/C2.png`，记录实际输出尺寸。
3. 圆角化：用 PIL 脚本对每张成品裁统一圆角（建议圆角半径 ≈ 画布边长 18–22%），另导出 256/64/32 px 的 ico 供 exe 与窗口标题栏使用。
