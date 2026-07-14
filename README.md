# 科学理财 Agent

行情数据 + 财务数据 + 新闻资讯，AI 交叉核实后呈现依据链，不做黑箱荐股。

## 设计原则

- **只摆事实，不下指令**：AI 输出的是"新闻核实 / 数据信号 / 不确定点"三段式分析，不会说"建议买入"这类话，判断权始终在使用者手里。
- **交叉验证优先**：新闻说了什么，先去财务/行情数据里核实站不站得住脚，而不是复述新闻情绪。
- **诚实的追踪记录**：不给分析打"准确率"分数（那样等于变相荐股），只客观记录"当时分析怎么说、价格是多少、后来涨跌了多少"，让使用者自己判断。

## 技术栈

- 数据源：[AkShare](https://akshare.akfamily.xyz)（免费开源，股票行情/财务数据）+ 东方财富个股新闻
- 分析：DeepSeek（`deepseek-v4-flash`）
- 前端：Streamlit
- 追踪记录：SQLite（`data/track_record.db`，不入库）

## 本地运行

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
# 在 .streamlit/secrets.toml 填入 DEEPSEEK_API_KEY
streamlit run app.py
```

## 已知限制

- 东方财富接口对高频请求有临时限流，`data_sources.py` 里已加了请求间隔和重试，正常使用不会触发。
- 只做 A 股个股分析，暂不支持基金/债券/期货。
