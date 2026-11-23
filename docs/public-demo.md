# 公开合同分析演示

公开演示运行独立的 FastAPI 应用 ``app.public_demo:app``。访客使用内置的虚构合同与服务清单。LangGraph 使用预设计划和内存检查点执行分析、证据复核与补证。每次请求独立运行，不需要配置数据库、模型密钥或访客账号。

## 部署到 Render

1. 登录 [Render 控制台](https://dashboard.render.com/)，选择 **New → Blueprint**，连接 GitHub 上的 ``analysis-agent`` 仓库和 ``main`` 分支。
2. 检查配置来自仓库根目录的 ``render.yaml``，服务计划为 **Free**，然后部署。演示不需要额外环境变量或 Secrets。
3. 部署显示 **Live** 后，用无痕窗口打开 Render 提供的 HTTPS 地址，并按下方步骤验证。

Render 免费服务闲置后会休眠，首次打开可能要等待约一分钟。[Render 免费服务说明](https://render.com/docs/free)

## 验证体验

1. 点击「开始分析」。应显示维护年费 **12,000 元**，引用 ``L2``。服务范围等待补证，巡检频率等待依赖完成。
2. 点击「补交服务清单」。应显示定期巡检、故障排查，以及巡检每月一次。下载的报告应引用 ``S1-L2`` 和 ``S1-L3``。
3. 重新开始，再选择「用现有证据生成报告」。部分报告应保留年费，并指出服务范围缺证和巡检频率未核对。

本地预览可从项目目录运行下面的命令，然后打开 [http://127.0.0.1:8001/](http://127.0.0.1:8001/)。

```powershell
& '.\.venv\Scripts\python.exe' -m pip install -r requirements-demo.txt
& '.\.venv\Scripts\python.exe' -m uvicorn app.public_demo:app --host 127.0.0.1 --port 8001
```
