# 采样接收材料（宽松集合 → 收紧模式）

本材料只用临时数据库（`tempfile.TemporaryDirectory`）建立，生产实现与仓库内数据库均未改动。

## 目标模式

```json
{
  "fields": {
    "sensor": "string",
    "value": "number",
    "sampled_ms": "integer",
    "source": "object"
  },
  "required": [
    "sensor",
    "value",
    "sampled_ms"
  ],
  "additional": false
}
```

正文只允许 `sensor`（字符串）、`value`（数值）、`sampled_ms`（整数）、`source`（对象）；前三项必填，`value` 统一为摄氏温度，`source` 保存原始记录与转换依据。

## 逐项处置

| 来源键 | 文档键 | 处置 | value(°C) | sampled_ms | 依据 |
| --- | --- | --- | --- | --- | --- |
| logger-a:r1 | `logger-a~r1` | 接收 | 21.5 | 1794960000000 | 同一 sensor、同一采样时刻另有来源 logger-b 的记录；以不同文档键并存，未互相覆盖 |
| logger-a:r2 | `logger-a~r2` | 接收 | 21.5 | 1794960000000 | 含义唯一 |
| logger-a:r3 | `logger-a~r3` | 待确认 | 20（候选） | — | 同一条记录内数值矛盾（value=20 与 reading.value=23），且没有可信的优先级说明；时间缺失；不能推断采样时刻 |
| logger-a:r4 | `logger-a~r4` | 待确认 | — | — | 值是布尔值；布尔不是温度，且被排除在数值之外；时间缺失；不能推断采样时刻 |
| logger-a:r5 | `logger-a~r5` | 待确认 | — | — | 值是 JSON null；目标要求数值；时间缺失；不能推断采样时刻 |
| logger-b:r6 | `logger-b~r6` | 接收 | 22 | 1794960000000 | 同一 sensor、同一采样时刻另有来源 logger-a 的记录；以不同文档键并存，未互相覆盖 |

## 精度说明

- 华氏换算采用精确十进制 `(F − 32) × 5 ÷ 9`，只在最后一步按 1e-9 °C 量化；70.7 °F 的精确结果为 21.5 °C，落库为 JSON 数值 21.5（IEEE-754 双精度可精确表示）。
- 摄氏字符串 `"21.5"` 解析为数值 21.5，无精度损失；非数值字符串不会被猜测为数字。
- `sampled_ms` 为 UTC 纪元毫秒：`+08:00` 与 `Z` 两种写法分别按各自偏移换算到同一时刻；缺时间或时区不明的记录一律不下推断。
- 独立核对不复用构建函数：数值用 `Fraction` 精确有理数复算，时刻用 `calendar.timegm` 复算，身份用 `system~record_key` 复算，并与落库值逐项比对。

## 导入、确认与收紧证据

- 宽松集合 `loose_samples` 保持修订 1，指纹 `f4360f302216ae68`；再次打开仍可读到原始记录：True。
- 新集合 `samples_readings` 先按宽松接收模式导入 3 条，提交修订 2 并逐键回读确认；随后模式代数由 1 升至 2。
- 收紧后 `diagnostics.inspect(deep=True)`：ok=True；收紧后内容指纹 `4166afc89e8a56e9`。
- 旧修订仍可读：True（logger-a~r1=21.5、logger-a~r2=21.5、logger-b~r6=22.0）。
- 收紧前对宽松集合直接应用目标模式被拒绝：`missing required field: sampled_ms`；收紧后写入旧形状文档被拒绝：`missing required field: sampled_ms`。
- 收紧后额外根字段被拒绝：`undeclared top-level fields: unit`；字符串温度被拒绝：`field value must be number`。
- 交付的 JSONL 可独立导入：在只含目标模式的空库中提交修订 1，共 3 条文档，内容与逐键回读结果一致：True。

## 独立核对

- 数值、时区、身份映射不一致项：无
- 待确认记录未写入任何集合，也不产生占位文档；补齐唯一取值与可信时间后可重新提交。
