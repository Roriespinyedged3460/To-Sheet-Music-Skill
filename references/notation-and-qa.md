# 谱面标准与验收

参考样式来自用户提供的 MuseScore 乐队谱：五件乐器、六条谱表、吉他/贝斯 TAB、键盘大谱表、五线鼓谱、A4 纵向总谱。随包模板已经移除原曲音符和元数据；只使用通用结构。不要把参考谱的 103 小节、82 BPM、调性、休止段落或音符密度作为新歌默认值。

## 制作与排版

- 顺序为主音吉他、节奏吉他、贝斯、键盘双谱表、鼓；用户另指定时遵从用户。保留真实的休止与统一小节线。
- 吉他/贝斯 TAB 必须有节奏符干、符尾或连梁、休止符和连音线；不能只显示无节奏的品位数字。相同演奏者不要同时出现两套重复播放的五线谱/TAB 轨。
- 键盘使用高低音双谱表与连接括号；鼓使用标准谱号和明确的鼓件映射。必要时添加简短鼓谱图例。
- 标题、速度、拍号、小节号、段落、必要和弦与奏法应清楚可读。具体大小节排布随音乐密度调整，不强迫每行固定四小节。
- 保留合适翻页与段落间隔，避免末页只剩单小节、页边裁切、TAB 品位碰撞、文字压住音符、异常空白或错误换行。

由[紧凑谱面事件](score-events.md)及 `scripts/score_events.py` 确定性生成 MusicXML 后导入 MuseScore。修正源事件并重新构建，不让模型改写整份 XML。必须检查导入后的真实谱式与音高，不能只靠 MusicXML 文本判断。转换器暂不支持的奏法需先扩展事件契约与测试，不可静默降级。

当前生成策略不输出左手指法数字：可演奏性规划报告内部仍可记录手指选择，但 `score_events.py` 生成的 MusicXML 不包含 `<technical><fingering>`，随包 `band-template.mscx` 的 `showTabFingering` 也设为 `0`。最终 TAB 只显示弦号、品位和节奏；视觉检查时不要把品位数字误认为手指标记。

MusicXML 的 `<technical><string>` 从最高音弦开始编号 1；`<staff-tuning line>` 的线号从底往上数，和前者不是同一方向。MSCX 的 `Note/string` 从最高音弦编号 0，`StringData/string` 按低到高存储。严格验证弦品与 MIDI 实际音高，不根据数组位置凭印象转换。

## 三类检查

### 音乐与原音频

全曲段落和长度一致；重点检查开头、结尾、每类段落、所有衔接、变速/拍号变化、标志性 riff 与独奏。核对 tempo map、弱起、调性、和弦、低音与鼓重拍。不把“生成了完整时长的文件”当作曲式内容完整。

记录实际采用的音频对照方式：有音频感知工具时制作对应片段试听；只有信号分析工具时保存音高、起音、chroma 等比较证据，不能宣称完成了听音审校。没有真实原始分轨时，不报告真实转录准确率或分离 SDR。

### 演奏与记谱

对齐各声部的每小节时值，正确处理延音线、附点、连音组、切分、弱起、跨小节音、反复和不同结尾。五件乐器之间没有不可能同时演奏的任务。

检查吉他/贝斯音域、调弦、弦品一致、一弦多音冲突、换把与跨度；键盘双手可同时演奏、音符释放合理；鼓同一瞬间的四肢组合可执行。`score_tools.py audit` 当前仅实现声部/谱表数量关系、小节数量、标准弦品音高和单和弦弦冲突检查，以及静音谱表、宽跨度提示；它不检查所有拍长、跨声部同时性、变调夹、泛音或特殊奏法。相关情况须由 agent 加上针对性验证，不把未经支持的例外硬改成普通音符。

吉他声部另按[乐句、指型与参考案例](arranging.md#吉他乐句指型与奏法)核对；主音吉他和贝斯的单音段用 `score_events.py build --report` 一次生成谱面及规划报告，再用同一份事件、配置和报告绑定导出；不必先单独运行 `check-playability`：

- 分开报告弦品合法性、整句动作可行性、音型/节奏保留程度和奏法表达；不能用其中一项通过替代其余项。`build` 内置的规划器与独立诊断命令 `check-playability` 都检查单音段的弦品、手指、手位、IOI、换把窗口和跨弦时间；和弦连接及特殊奏法仍会明确列为未验证。
- 列出最困难连接的声部、小节、前后弦品、手位、节奏与奏法上下文，评估手部路径、可用时间和所需时间；单音序列也必须检查，不只检查单和弦跨度。
- 参考谱先确认调弦、变调夹、实际音高、拍号、速度单位、弱起和段落范围，再对齐起音序列。合并延音线连接的同一发声，保留真正的重复拨弦；逐项区分指法差异、转录疑点与明确的难度改编。
- 独奏参考与乐队编配比较时，先合并承担该音型的声部；不能仅因主音缺音、旋律音延长或指型不同判为失误。按实际音高和起音检查承接，将跨声部保留、移八度/音区改编、起音变化、持续时间变化和合并后缺失分别报告；同音持续不等于再次起音。对 `let ring` 按各乐手实际弦占用检查余音；检查关键推弦、回落、滑音、击勾弦、揉弦与闷音是否由任何相关声部承接。
- 指法重排需验证音高、起音、时值和延音连接未被意外改变；开放弦与低把位不是自动错误。没有音频对齐或乐手试弹时，明确哪些结论只来自谱面对照，不宣称原曲准确或已验证手感。

### 文件和视觉

1. 用 `export` 一次生成定稿三格式：MusicXML 仅导入一次，已有 MSCZ 直接复制；PDF 和 MIDI 均从交付 MSCZ 导出。确认定稿可打开、可编辑，无需为此再另存一次。
2. MIDI 解析成功，tempo map、有效音符、声部映射、时长正确；检查误移八度、鼓被导成钢琴、TAB 重复播放、空音轨、反复展开长度。MIDI 文件非空或有 `MThd` 头只能证明基本格式。
3. 默认只对最终 PDF 渲染并逐页检查一次字体、谱号、TAB 数字、连梁、连音线、节奏和版面。后续只查看受影响页面（含分页变化导致的后续重排页面），相同页面哈希不重复检查。用户明确免格式校验时跳过渲染和视觉审核，记录未检查，不补做隐藏审核。
4. 导出后若修改 MSCZ，重新从它导出 MIDI 和 PDF。脚本的哈希 manifest 只记录同次导出来源，不保证后续手动编辑后仍同步。
5. 确认 TAB 没有额外的左手指法标记；品位数字本身仍应保留。

```text
python /path/to/skill/scripts/score_tools.py export --score work/score-v1.musicxml --events work/events.json --playability-report work/playability.json --rhythm-evidence work/rhythm-evidence.json --out-dir work/export-v1 --name song_band --musescore /actual/MuseScore
只在需要视觉检查时运行：
pdftoppm -png -r 120 work/export-v1/song_band.pdf work/qa/page
```

中间排版稿需要预览时，只调用 MuseScore 将当前版本 MusicXML 导出为 PDF，不生成整套三格式。事件、报告、谱面和预览都从同一版本目录派生。校验发现错误时修改源谱，重新检查受影响内容。常规问题由 agent 内部修正，不因每个跨度提示或小节调整向用户询问。内部报告应精确说明已检查的内容与仍未证明的内容。

## 交付门槛

三个格式都可用、同源、整曲完整、符合指定编制与难度；音乐核心没有已知未解决的重大问题，正式谱面可以排练。没有充分证据时用“可演奏改编”描述实际成果，不宣称“原曲零误差”或“已逐音审校”。若核心问题经过有依据的修正仍不能解决，简短说明具体问题及需要的最小补充信息；不要把明显有问题的文件包装成定稿。

## 节奏证据契约

`scripts/rhythm_audit.py` 比较独立证据与紧凑事件；不从待验收事件生成“正确答案”，不自动把复杂节奏拉直。证据来自音频起音/能量、鼓点锚点，或已经与录音对齐的参考谱。音高中值相符不证明起音和休止相符。

```json
{
  "version": 1,
  "anchors": [
    {"at": [1, 0], "seconds": 0, "source": "原音频第一重拍"},
    {"at": [2, 0], "seconds": 3, "source": "原音频下一小节重拍"}
  ],
  "phrases": [
    {"id": "theme", "parts": ["lead"], "start": [1, 0], "end": [2, 0],
     "onsets": [0, 1, 2, 3], "durations": [1, 1, 1, 1],
     "source": "与录音对齐的参考乐句或独立起音分析"}
  ]
}
```

- `at/start/end` 是 `[小节号, 四分拍偏移]`。`onsets` 相对于该乐句起点，必须递增；`durations` 可选，和起音一一对应，跨越检查区间的尾音在区间末尾截取。默认起音/时值容差 0.01 四分拍、音频锚点容差 0.08 秒；可用顶层 `tolerance_quarters/tolerance_seconds` 按证据分辨率调整，不能为让错误稿通过而放宽。
- `parts` 可列承担该音型的多个声部，比较其起音并集；同一时刻的和弦只算一次攻击，跨小节延音不算再次攻击。多声部同起音时，时值取选中声部在检查范围内的最长持续；需要逐声部检查时分别列乐句。
- 缺少锚点、缺少乐句、`onsets:null` 或空检测结果都返回 `incomplete`。经能量和上下文确认的真实整段休止，可使用 `onsets:[]` 和 `silence_confirmed:true`，并在 `source` 说明证据。检测器未检出不能自动视为静音。
- 不一致返回 `needs_repair`，列出漏起音、多余起音、时值差和秒级锚点残差。容差外的位移会同时表现为漏起音与多余起音；报告不自动猜测二者是否为同一个音。真实短休止只要与证据一致就允许。
- `passed` 只代表列明范围内符合证据，始终保留 `musical_accuracy_verified:false`。必须在证据中列出已知问题和标志性乐句，不能只检查容易的部分。对未知片段保留 `onsets:null`，补充独立分析后再替换。
- 信号音高比较另用 `summarize_coverage` 汇总每个必检音符的 `checked/mismatch/insufficient_evidence` 记录；即使差异列表为空，只要存在未覆盖项就返回 `incomplete`。pYIN 的 `voiced_prob` 是有声概率，不是音高正确率；低值、空数组与 NaN 都不能直接当休止。

```text
python scripts/score_tools.py export --score work/final.mscz --events work/events.json --rhythm-evidence work/rhythm-evidence.json --playability-report work/playability.json --out-dir work/exports-v1 --name song_band --musescore /actual/MuseScore
```

带 `--events` 的正式导出要求 `--rhythm-evidence`，每次调用现场检查一次，避免使用过期通过报告；后续原生谱结构检查不重复计算同一份事件/证据。独立 `rhythm_audit.py` 和 `score_tools.py audit` 留作问题诊断，不是正式导出的固定前后步骤。只要证据缺失、未覆盖或不一致就停止发布导出目录。旧的无事件文件转换接口仍可用，但其节奏状态为未验证，不是本 skill 的正式交付流程。原生谱与源事件是否一致仍需独立 MIDI/时值比较；证据哈希只绑定事件和证据，不能代替这种比较。

## 依据

- [librosa pYIN](https://librosa.org/doc/0.11.0/generated/librosa.pyin.html)：输出基频、有声标志及有声概率；不是起音或静音的真值。
- [librosa onset detection](https://librosa.org/doc/0.11.0/generated/librosa.onset.onset_detect.html)：从起音强度峰取得候选；空检测不证明真实静音。
- [MusicXML string](https://www.w3.org/2021/06/musicxml40/musicxml-reference/elements/string/)
- [MusicXML staff-tuning](https://www.w3.org/2021/06/musicxml40/musicxml-reference/elements/staff-tuning/)
- MSCX 弦序和默认样式已通过用户参考文件、原生导出与合成音符检查交叉验证。
