# T-121 对象存储适配与上传病毒扫描

**版本**：V1.0（生产门禁，可提前）　**依赖**：T-002　**PRD**：NFR-006（存储路径带 tenant_id）、NFR-009（上传病毒扫描）、NFR-018（存储插件化）

## 范围

1. `ObjectStore` 接口与实现：本地文件系统（开发/CI）与 S3 兼容（boto3）；对象键统一 `tenants/{tenant_id}/{domain}/{id}/...`；签名下载 URL；生命周期删除。对象存储产品选型属于部署决策，注意 technical-plan 的许可门禁（不默认引入 MinIO 作为分发组件）。
2. 迁移已有的本地暂存用法（文档暂存 T-031、Skill 包 T-074、导出文件 T-086、Logo T-090、工单附件 T-101）到该接口。
3. 病毒扫描：`MalwareScanner` 接口 + ClamAV（clamd）实现，Compose 可选 profile（内存约 1 GB，开发机默认不启动）；上传后状态 `scanning` → `clean` / `infected`（隔离，不可下载、不可转交 RAGFlow）；扫描器不可用时按配置“阻塞等待”（生产默认）或“跳过并标记”（开发默认）。
4. 文件类型校验统一放在这里（扩展名 + 魔数嗅探 + 大小）。

## 验收标准

- [ ] 测试：EICAR 测试文件被识别为 infected 并拒绝后续处理；不同租户前缀不可互访（签名 URL 不能越权）。
