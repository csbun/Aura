# Immich 中文和 AI：照着做

适用版本：`v3.3.5`。本项目当前已经锁定这个版本。

只按下面顺序操作。

## 第 1 步：启动 Immich

在 Aura 项目目录执行：

```bash
docker compose -f docker-compose.yml -f docker-compose.immich.yml \
  --profile immich up -d
```

确认三个服务都在运行：

```bash
docker compose ps
```

应看到 `immich_server`、`immich_machine_learning`、`immich_postgres` 正常运行。

## 第 2 步：把 Immich 改成中文

1. 打开 Immich，登录账号。
2. 点击右上角头像。
3. 点击 **Account Settings**。
4. 找到 **Language**，选择 **简体中文**。
5. 刷新页面。

语言是每个账号单独设置的，每个账号都要做一次。

## 第 3 步：开启中文 AI 搜索

1. 用管理员账号打开 **Administration → Settings → Machine Learning Settings**。
2. 确认 **Machine Learning、Smart Search、Facial Recognition、OCR** 都是开启状态。
3. 打开 **Smart Search**，把 **Model Name** 改成下面这一项：

   ```text
   ViT-B-16-SigLIP2__webli
   ```

4. 点击保存。
5. 打开 **Administration → Jobs**。
6. 点击 **Smart Search** 右侧的 **All**，等待任务完成。
7. 如果人物或文字搜索还没有结果，再依次点击 **Face Detection、Facial Recognition、OCR** 右侧的 **All**。

`ViT-B-16-SigLIP2__webli` 是 Immich `v3.3.5` 支持的多语言模型，官方简体中文评测召回率为 66.84%，峰值内存约 3.0 GiB，适合当前单 worker、自动释放模型的配置。它支持中英文混合搜索，不依赖当前账号语言。

如果主机内存充足、更加重视中文搜索准确率，可以改用 `nllb-clip-large-siglip__v1`；它的中文评测召回率为 79.7%，但峰值内存约 4.2 GiB，并且需要把账号语言设为简体中文。

第一次使用时，Immich 会自动下载模型到 `immich_model_cache`，不需要手工下载。下载期间搜索可能暂时没有结果，请等待下载完成。

完成后，在 Immich 搜索框测试：

```text
海边的孩子
春节聚餐
杭州夜景
```

能返回相关照片后，Aura 的自然语言相册才具备中文画面搜索能力。

## 第 4 步：生成中文地点信息（可选）

使用 `immich-geodata-cn` 给 Immich 自带的反向地理编码替换中文地理数据。它通过 Compose 挂载数据，不另起服务，也不直接写 PostgreSQL。

### 4.1 下载中文数据

```bash
bash scripts/prepare_immich_geodata_cn.sh
```

默认下载中国省、市、区县的增强数据。要换成更细或更小的数据包，把 Release 中的文件名作为参数传入。

### 4.2 带中文数据重建 Immich

```bash
docker compose -f docker-compose.yml -f docker-compose.immich.yml \
  -f docker-compose.samples.yml \
  -f docker-compose.immich.geodata-cn.yml \
  --profile immich up -d --no-deps immich-server
```

如果不使用样本，去掉 `-f docker-compose.samples.yml`。

启动后检查日志：

```bash
docker logs immich_server 2>&1 | grep 'geodata records imported'
```

然后在 Immich 管理员页面打开 **Administration → Jobs → Metadata Extraction**，点击 **All**，刷新已有照片的地点信息。以后新照片会使用这套中文数据自动处理。

不再需要额外运行地理编码服务；Immich 会使用挂载的数据完成地点处理。地图底图的中文标注仍在 **Map & GPS Settings → Map Settings** 中配置。

该项目主要覆盖中国大陆、港澳台，并实验性支持部分海外地区；它不是全球中文地名数据库。[项目说明](https://github.com/ZingLix/immich-geodata-cn)

官方依据：[v3.3.5 发布记录](https://github.com/immich-app/immich/releases/tag/v3.3.5)、[中文 Smart Search](https://docs.immich.app/features/searching/)、[用户语言设置](https://docs.immich.app/features/user-settings/)、[反向地理编码](https://docs.immich.app/features/reverse-geocoding/)。
