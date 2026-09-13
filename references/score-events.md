# 紧凑谱面事件 v1

编配阶段只维护 JSON。运行 `scripts/score_events.py`，不要把完整 MusicXML/MSCX 或模板读入上下文，也不要每首重写序列化脚本。检查时只读摘要、相关小节和审计错误；默认只在定稿后查看实际 PDF，用户明确免格式校验时跳过。

```json
{"version":1,"title":"歌曲名","tempo":80,"time":[4,4],"key":0,
 "bars":[{},{}],
 "tracks":[{"bar":1,"part":"bass","events":[[0,48,1],[2,50,1]]},
           {"bar":1,"part":"keys_rh","events":[[0,[60,64,67],4]]},
           {"bar":1,"part":"drums","events":[[0,[36,42],0.5]]}]}
```

- 时间单位都是四分音符拍。小节从 1 开始，起点从 0 开始；事件为 `[起点,MIDI音高或和弦数组或null,时长]`。空白自动补休止，不能因此省略有声音的声部。
- `part`：`lead`、`rhythm`、`bass`、`keys_rh`、`keys_lh`、`drums`。键盘两行合成同一乐器的双谱表；其余对应五件编制。每个小节/part 最多一条记录，事件按起点排序。
- 同时同长音写和弦数组；每行不能重叠。跨小节长音自动拆分并用延音线连接；末音不能越过最后一小节。
- `bars` 明确列出全曲所有小节。每项可设置 `time:[3,4]`、`tempo:90`、`length:1`（弱起或不满小节）、`section:"A"`、`chord:"F7"`。拍号沿用；tempo 为该小节开始的新速度。chord 只生成可见文字，不推断任何伴奏音符。
- `key` 是调号升降数量 -7..7，默认 0。`tunings` 可按 `lead/rhythm/bass` 覆盖低到高的空弦 MIDI 音高。默认标准调弦，指板范围 0..24。
- 默认自动排版；若逐页检查发现末页孤立小节等问题，可在相关 bar 设置 `new_system:true` 或 `new_page:true`，通过事件重新生成，无须模型编辑 XML。
- `lead` 与 `bass` 的单音段使用带内部手指约束和动作时间的全局路径；`rhythm` 保留原有软代价 TAB 行为。三者都连接跨小节、休止及段落边界两侧的手位；多音和弦仍沿用逐事件选择并标记为未覆盖。内部规划结果只用于可演奏性报告，生成 MusicXML 时仅写弦号和品位，不写 `<technical><fingering>`，因此最终谱面不会显示 1/2/3/4 手指数字。可指定第四项 `[[弦号,品位],...]`，对应各音高，最高音弦为 1。例如贝斯 `[0,48,1,[[1,5]]]`。手动指定的弦品保持不变，并作为路径规划的固定候选；不匹配的弦品或无可用弦组合直接报错，不自动改音。
- 普通事件支持到三十二分音符及单附点，拍值为 1/8 的倍数（可用 `"3/2"` 表示有理数）；显式三连音按下节规则表示。鼓采用明确的 GM 音号。未知字段、未支持鼓件、下述范围以外的连音组/特殊奏法、多声部不等长重叠不能悄悄丢弃或近似；须先扩展契约与转换器测试。

## 哑音、混合闷弦、三连音与倚音

事件可添加第五项 options（无手动 TAB 时第四项写 null）。以下为当前明确支持的范围，不把特殊奏法当普通音高或休止：

- 纯哑音：`[0,null,0.25,[[6,0]],{"dead":true}]`。null 表示无确定音高，TAB 指定发声弦；MusicXML 用 x 符头。导出器会核对实际 MSCZ 中的弦/品及哑音数量，再设置原生 dead/play 标记。PDF/MSCZ 保留 x，MIDI 不合成这类噪音，不能宣称 MIDI 重现了闷弦音色。
- 实音和闷弦同时拨奏：`[0,[48,60],0.5,[[5,3],[3,5]],{"dead_tabs":[[4,0]]}]`，同一根弦不能既是实音又是闷弦。哑音和弦连接仍在动作模型的未覆盖范围，不能因此报告全曲可演奏性通过。
- 三连音：`[0,64,"1/3",null,{"tuplet":[3,2]}]`。实际时值使用精确分数字符串；每组三个等长音符/休止须显式连续写出，组内不可跨小节。转换器保存 time-modification，按需提高 MusicXML divisions，不把 1/3 近似成 0.375。不支持的连音比例和不完整组直接报错。
- 键盘前倚音：主音事件上附 `{"grace":[[83,0.25],[86,0.25]]}`，每项为 MIDI 音高与书写时值；不占小节拍长。鼓前倚音使用相同结构，音高为 GM 鼓件。MuseScore 的装饰音回放可能轻微调整相邻音的起止时间，MIDI 验收要单列这些音，不能计作漏音。弦乐倚音、复合连音和连续推弦仍须另作明确表示与检查。

直接导入中间 MusicXML 的软件可能忽略哑音播放限制，正式交付必须走 `score_tools.py export`，不能把中间 MusicXML 的试听当作最后 MIDI。

## 弦乐声部的单音可演奏性规划

生成前先按实际时间排序，对每位乐手的单音段用动态规划联合选择弦、品和手的把位；内部结果随可演奏性报告保存，MusicXML 只保留弦号与品位。音高、起止时间和手动弦品不变，跨小节长音拆出的延音保持同一弦品；不同乐手的音符不会参与彼此的手位规划。

- 每个音枚举调弦下的可用品位和 1～4 指候选；手的把位单独表示，不把相邻音品位差直接当成换把距离。默认食指到小指覆盖三品，并允许小指额外伸展一品；这是简化模型，不是具体人体工学判定。选中的手指只保存在检查报告中，MusicXML 不写入 `<fingering>`；导出谱面因此不会出现额外的指法数字。
- 联合比较换把距离、跨弦和伸展代价；状态保留最近换把的方向与时间，中间不换把的音不会清空历史。默认一秒窗口内，再次换把增加代价，反方向折返另加代价，惩罚随时间衰减。历史以换把后音的起点记录，是保守的动作时刻近似，不是完整动作轨迹。
- 按速度变化积分计算移动时间：按弦音只能使用释放后到下个起音之间的空隙；空弦允许左手自其起音后提前移动。默认换把需求为 `0.05 + 0.02 × 品位移动数` 秒，跨弦需求为 `0.02 × 跨越弦数` 秒；前者与实际移动窗口比较，后者与 IOI 比较。硬约束失败会产生 `needs_repair`，不再用最小时间下限把零窗口动作伪装成可行。
- 小节线、`section` 和长短休止不切断路径；后一句的固定弦品可以影响前一句收尾，休止的实际秒数则影响连接代价。显式休止与省略事件形成的空白等价。多音和弦仍结束单音规划段，和弦两侧的连接暂不由此模型优化。
- 空弦不会将手的位置重置到第 0 品。算法不强制电吉他使用中高把位；风格和奏法要求可通过手动弦品表达。
- 权重、指法限制、舒适把位和时间参数集中在独立配置 JSON 的 `profiles.lead`（也接受 `guitar` 别名）与 `profiles.bass` 中；默认 `comfortable_fret_range` 为全指板且舒适度权重为零，`open_string_preference` 也为零，不强制高把位或禁止空弦。参数是待乐手校准的启发式值；报告会记录 `config_sha256`、实际参数和 `not_musician_calibrated`。

可用配置示例：

```json
{"version":1,"profiles":{"lead":{
  "allowed_fingers":[1,2,3,4],
  "disabled_fingers":[ ],
  "comfortable_fret_range":[5,12],
  "open_string_preference":0.1,
  "costs":{"comfort":0.2,"open":0.1},
  "timing":{"shift_base_seconds":0.05,"shift_per_fret_seconds":0.02,
             "string_per_crossed_seconds":0.02},
  "allow_same_finger":false,
  "same_finger_requires_same_fret_or_string":true
}}}
```

只读可演奏性检查会输出 `passed`、`needs_repair`、`incomplete` 或 `computation_incomplete`；`check_scope` 会列明已支持的主音/贝斯单音段、沿用旧行为的节奏吉他，以及和弦连接和特殊奏法等未覆盖范围。报告列出每个连接的 IOI、可用时间、所需时间、前后把位、分项成本和最难连接。

开头 `69,67,69,72,74,69` 的连续单音案例，会提前使用 2 弦 10、8、10 品，避免逐音选最低品导致的 1 弦来回跳把。

方法参考：[From MIDI to Rich Tablatures](https://arxiv.org/html/2407.09052v1)，本实现只采用弦品候选、手把位状态和路径代价的基本思路。

```text
python scripts/score_events.py build --events work/events.json --config work/playability-config.json --output work/score-v1.musicxml --report work/playability-final.json
python scripts/score_tools.py export --score work/score-v1.musicxml --events work/events.json --config work/playability-config.json --out-dir work/exports-v1 --name song_band --musescore /actual/MuseScore --playability-report work/playability-final.json --rhythm-evidence work/rhythm-evidence.json
```

默认按上面两步执行。`build --report` 已包含整句可演奏性规划，不再预先运行同等规划的 `check-playability`。

以下命令仅按需诊断或修复，不逐项例行运行：

```text
python scripts/score_events.py inspect --events work/events.json --bar 17
python scripts/score_events.py check-playability --events work/events.json --config work/playability-config.json --report work/playability-check.json
python scripts/score_events.py repair-playability --events work/events.json --constraints work/constraints.json --output work/events-repaired.json --report work/repair-report.json
```

`build` 可用 `--config` 和 `--report`，输出报告会绑定事件、配置和 MusicXML 哈希；`score_tools.py audit/export` 可用 `--events`、`--config` 复核哈希，若报告另含 `score_native_sha256` 也会核对 MSCZ/MSCX 内容；输出文件须不存在。`repair-playability` 首期只处理 `lead`/`bass` 中约束明确列出的单音事件，默认所有事件受保护，最多尝试三种策略：重排指法、局部缩短/移八度、删除可调整装饰。候选必须减少动作问题且不引入新动作违规才接受；这不是音乐节奏已经正确的结论。修复后还需按[节奏证据契约](notation-and-qa.md#节奏证据契约)复核起音与持续，带事件的正式导出要求 `--rhythm-evidence`。转换器不负责自动扒谱或自行改变核心音乐约束。

性能基准可运行 `python scripts/benchmark_playability.py`，默认测量 128、512、1024 音序列的耗时、最大状态/转换数和 `tracemalloc` 峰值内存。资源限制触发时报告 `computation_incomplete`，不能改报为无解或通过。

格式依据：[MusicXML note 顺序及延音](https://www.w3.org/2021/06/musicxml40/musicxml-reference/elements/note/)、[midi-unpitched 的 1..128 编号](https://www.w3.org/2021/06/musicxml40/musicxml-reference/elements/midi-unpitched/)。
