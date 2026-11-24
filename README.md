# 旧版地名对照表供数服务

编目员上传 JSON 登记发布号、行号、旧名、今名与依据档号；档号可撤档；读者按发布号
流式下载 CSV。数据存 SQLite，可用 `docker compose` 启动。

## 启动

```bash
docker compose up --build -d
# 打开 http://localhost:8000/
```

数据保存在命名卷 `gazetteer-data` 的 `/data/archive.sqlite3`。可选环境变量：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `ARCHIVE_DB` | `/data/archive.sqlite3` | SQLite 文件路径 |
| `SECRET_KEY` | `change-me-in-production` | 会话密钥 |
| `PORT` | `8000` | 容器内监听端口 |

## 提交格式

`POST /publish`（也接受 `JSON` 内容类型或表单中的 `json_file` / `payload` 字段）。

```json
{
  "publish_no": "GZ-2026-001",
  "source_name": "某地方志办公室",
  "rows": [
    {"line_no": "1", "old_name": "北平", "new_name": "北京",
     "evidence": [["A-001", "A-002"], ["B-010"]]},
    {"line_no": "2", "old_name": "迪化", "new_name": "乌鲁木齐",
     "evidence": ["C-001"]}
  ]
}
```

- `publish_no` 必须提供且全局唯一；行号在同一发布内不可重复。
- `evidence` 是“一组或多组”档号的数组；单组也可写成扁平的档号数组或单个字符串。
- 一份 JSON 是**一个发布批次**。任一行缺少完整组（组内全部档号都未撤档）就整批退回，
  不写入任何数据。

字段也接受中文键名：`发布号`、`行`、`行号`、`旧名`、`今名`、`依据档号`。

## 撤档

```bash
curl -X POST http://localhost:8000/withdraw \
  -H 'Content-Type: application/json' \
  -d '{"archive_no":"A-001"}'
```

- 同一档号重复撤档只登记一次（`newly_withdrawn` 为 `false`）。
- 写入失败时事务回滚，沿用原撤档状态。
- 撤档按档号全局生效，影响此后所有发布校验与下载。

## 下载

`GET /download/<发布号>.csv`（也提供 `GET /csv/<发布号>`）。

- 只输出至少有一个完整组的行，每行附上**按登记顺序第一个完整组**的全部档号，以 `;` 分隔。
- 缺少完整组的行整行略去。
- 边取数边生成：按主键分批读取条目、每批立即写出，内存不随表的行数增长。
- 一次下载在开始取数时读取撤档登记的最大编号作为快照界线，整个文件只认界线以内的撤档
  记录；下载期间新发生的撤档不影响本次文件，之后的下载才生效。

CSV 列为：`行号,旧名,今名,依据档号`。

## 测试

```bash
python -m pytest -q tests
```

## 备注

- SQLite 使用 WAL 模式，写入（发布、撤档）与流式读取互不阻塞。
- 容器内单 gunicorn worker 多线程，`--timeout 0` 以免长时间下载被中断。
