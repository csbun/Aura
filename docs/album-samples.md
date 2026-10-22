# 个人相册测试照片：从下载到只保留成品

推荐入口是 `scripts/prepare_album_samples.py`。它依次下载 CITIES/PIPA、恢复拍摄日期、查询城市近似坐标、无损写入 EXIF，最后清除原图、JSON、预览图和下载缓存，只输出改写后的照片。

## 一条命令执行完整流程

需要 Python 3.11+、`curl`，以及支持文件锁的 Linux/macOS 环境。在仓库根目录执行：

```bash
uv pip install --python .venv/bin/python -r scripts/requirements-album-samples.txt
.venv/bin/python scripts/prepare_album_samples.py --count 500
```

Hugging Face 主站不可达时，显式选择公开镜像：

```bash
.venv/bin/python scripts/prepare_album_samples.py \
  --count 500 --hf-endpoint https://hf-mirror.com
```

`--count 500` 表示**每个数据集下载 500 张候选照片**，不是保证最终每套保留 500 张。PIPA 只保留能恢复日期且完成 EXIF 写入的照片，不使用虚构时间凑数。

最终目录没有清单、JSON、预览图或缓存：

```text
sample-photos/
  cities/<来源集合_拍摄日>/<照片>.jpg
  pipa/<人物组>/<照片>.png
```

本次实际结果为 CITIES **500 张**、PIPA **442 张**，合计 **942 张**。PIPA 的 55 张官方日期为空、3 张未通过匹配校验，未进入最终目录。以后运行的数量可能随可用下载链接和匹配结果变化。

重复执行相同命令会检查现有照片的日期、GPS、来源标记和数量范围，并计算哈希排除重复；通过后直接退出，不重新下载。清单删除后，脚本无法仅凭目录判断用户是否又手工删除了部分 PIPA 成品。当前目录已经是最终结果，可直接使用。改变候选数量时应指定新的 `--output`，例如 `--output sample-photos-new --count 1000`。

## 处理阶段

| 阶段 | 脚本 | 行为 |
|---|---|---|
| 下载 | `download_album_samples.py` | 每套下载指定数量，检查可读性、尺寸，按 SHA-256 去重并保留来源清单 |
| PIPA 时间恢复 | `restore_pipa_dates.py` | 对齐官方实例索引和时间数组，用框几何与裁剪图内容核对副本对应关系 |
| EXIF 写入 | `enrich_album_exif.py` | CITIES 写入日期和城市级 GPS；PIPA 写入已恢复日期；逐张验证像素未改变 |
| 输出及清理 | `prepare_album_samples.py` | 校验来源/成品校验和、日期、GPS和完整性，只复制成品照片，发布后删除整个工作目录 |

默认工作目录是输出目录旁的 `.sample-photos-work/`，已被 Git 忽略。它在成功结束时删除；任何下载、匹配或校验错误都会中止并保留工作目录，便于修复后运行相同命令续跑。清理发生在两套成品都已校验并成功输出之后。若程序在输出完成后、清理前中断，下次运行会核对工作目录和成品一致后完成清理。

脚本只清理带有自身标记、且配置相符的工作目录。非空的未知目录、符号链接、嵌套的工作/输出路径和不同内容的既有输出会报错，不会覆盖或删除。工作目录与输出必须处于同一文件系统。工作目录有文件锁，避免并发运行同一任务。

常用选项：

| 选项 | 默认值 | 含义 |
|---|---|---|
| `--output` | `sample-photos` | 最终纯照片目录 |
| `--work-dir` | 输出目录旁的隐藏工作目录 | 中间文件目录，成功后自动删除 |
| `--count` | `500` | 每套候选照片数量 |
| `--hf-endpoint` | `https://huggingface.co` | 可改为 `https://hf-mirror.com` |
| `--workers` | `6` | CITIES 下载并发 |
| `--per-group` | `50` | 每个来源组的选图上限 |
| `--min-edge` | `500` | 最短长边像素；短边还须至少 200px |

## CITIES：连续相册、日期与城市坐标

来源为 [CITIES 官方项目](https://hucvl.github.io/visual-storygraphs/)的 [ZIP 索引](https://hucvl.github.io/visual-storygraphs/yfcmmf00m-cities.zip)。按六座城市分配配额，优先选择同一来源集合、同一拍摄日的连续照片，失效的组由下一组替补。获取 Flickr 1024px 衍生版，失败时使用索引版本；不声称取得相机原图。

索引包含 `taken_dt`、城市、作者和许可证等信息，但**没有精确经纬度列**：

- `DateTimeOriginal` 使用索引中的日期时间读数。虽然源字符串带 `Z`，相机时区并未独立确认，因此不推算或补写 UTC 偏移。
- 使用 [Open-Meteo Geocoding API](https://open-meteo.com/en/docs/geocoding-api)，按城市名加国家代码查询城市代表点，写入 `GPSLatitude` / `GPSLongitude`。
- 同一城市使用相同坐标；这是**城市级近似**，适合城市分组测试，不代表拍摄现场位置或市内旅行轨迹。
- 查询结果在处理阶段缓存，每个城市无需逐张重复查询。Open-Meteo 免费接口用于非商业用途；商业使用见其服务条款。

EXIF 的 `GPSProcessingMethod` 和注释标记近似性质、GeoNames / Open-Meteo 来源及坐标数据署名。

## PIPA：从副本恢复官方拍摄日期

照片取自 [MVEB-test 的 PIPA 子集](https://huggingface.co/datasets/HugC/MVEB-test/tree/8126e36012415238dff1429fd20c39552ddb1661/PIPA)，版本固定为 `8126e36012415238dff1429fd20c39552ddb1661`。只选择 **candidate 完整照片**，不把 query 人脸裁剪图作为最终照片。按副本的人物标识分组，使同一个人重复出现；这些组不等同于原始 Flickr 相册。

副本没有保留原始照片 ID、相册 ID 和日期。[PIPA 官方项目页](https://www.mpi-inf.mpg.de/departments/computer-vision-and-machine-learning/research/people-detection-pose-estimation-and-tracking/person-recognition-in-personal-photo-collections)另外提供：

- [data_timestamp.mat](https://datasets.d2.mpi-inf.mpg.de/oh15iccv/data_timestamp.mat)：`date_vec` 为 63,188 × 6 数组，依次是年、月、日、时、分、秒。全零表示缺失。
- [index.txt](https://datasets.d2.mpi-inf.mpg.de/oh15iccv/index.txt)：相同顺序的 63,188 个人物实例，包含相册 ID、照片 ID 和人物框。

同一照片可能包含多个实例，不能把副本照片顺序直接当作官方时间数组顺序。恢复脚本同时核对人物编号、官方框尺寸、坐标处的照片区域与 query 裁剪图内容。由于裁剪图经过重新压缩，要求像素平均绝对误差小于 10/255、相关系数大于 0.95，且只能对应一个官方照片 ID。弱匹配、歧义匹配、日期缺失或冲突均不回填。它是保守的内容匹配，不是原始 ID 的直接查表。

处理阶段保存匹配证据、官方索引行号及源文件校验和供核验。本次 500 张中有 497 张通过映射，其中 442 张有官方日期。日期时区未指定，因此不补写偏移；PIPA 没有可靠来源城市，GPS 保持缺失。

当前恢复脚本读取固定 MVEB 版本的第一个 query media 分片，覆盖本次 500 张所需查询。扩大候选规模后，位于其他分片的 query 会被安全跳过，可能进一步减少最终保留数量。

## EXIF 写入和保留信息

JPEG 只改写 EXIF APP1 段；PNG 只改写 eXIf 块，保留其他段/块和压缩图像数据。每张图片都回读 EXIF、比较解码像素，确认没有重新压缩造成的变化。已有拍摄时间或 GPS 不被近似值覆盖。

新生成的 EXIF 注释包含日期/坐标来源、近似标记，以及已有的图片来源、作者、许可证和 PIPA 匹配证据。若原来有用户注释，其内容也保存在新注释内。这样删除旁边的 JSON 后，来源说明仍随照片保留。

CITIES 的许可证按照片记录；MVEB 数据集卡标注 CC-BY-NC-SA-4.0，但未提供逐张原始 Flickr 许可证映射。这些照片用于本地测试，不应被当作统一可商用素材库。

## 分步调试与验证

需要检查中间数据时，可直接执行各阶段脚本，并使用单独的临时目录。底层脚本的默认输出仍是含清单的工作格式，**不要把它们直接指向已经清理好的最终照片目录**：

```bash
.venv/bin/python scripts/download_album_samples.py \
  --output /tmp/aura-album-debug --count 500 --hf-endpoint https://hf-mirror.com
.venv/bin/python scripts/restore_pipa_dates.py \
  --source /tmp/aura-album-debug/pipa --hf-endpoint https://hf-mirror.com
.venv/bin/python scripts/enrich_album_exif.py \
  --source /tmp/aura-album-debug/cities --output /tmp/aura-album-debug/cities-exif --geocode
.venv/bin/python scripts/enrich_album_exif.py \
  --source /tmp/aura-album-debug/pipa --output /tmp/aura-album-debug/pipa-exif
```

分步执行不自动清理调试目录。推荐主入口负责正常使用时的输出与清理。

离线测试：

```bash
.venv/bin/python -m unittest \
  scripts.test_download_album_samples scripts.test_restore_pipa_dates \
  scripts.test_enrich_album_exif scripts.test_prepare_album_samples
```

覆盖下载去重和续跑、HTTP 分块读取、裁剪图排除、官方时间行对齐、歧义匹配拒绝、JPEG/PNG 像素与非 EXIF 数据保留、最终纯照片输出、清理与失败恢复。测试使用临时图片和模拟下载，不访问外网。
