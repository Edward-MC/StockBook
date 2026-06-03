# «衡» StockBook —— 把应用钉在 Python 3.9 上、一条命令可跑的镜像。
# 默认精简:只装核心运行时依赖,不含 RAG 那一坨(fastembed→onnxruntime 很重)。
# 需要 RAG:构建时传 --build-arg INSTALL_RAG=1(见 docker-compose.yml 注释)。
FROM python:3.9-slim

# 不写 .pyc、日志不缓冲(容器里实时看输出)。
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# 先拷依赖清单单独装,利用 Docker 层缓存:代码改动不会让依赖层失效。
COPY requirements.txt requirements-rag.txt ./

# INSTALL_RAG=0(默认)只装核心;=1 额外装 RAG 依赖。
ARG INSTALL_RAG=0
RUN pip install --no-cache-dir -r requirements.txt \
    && if [ "$INSTALL_RAG" = "1" ]; then pip install --no-cache-dir -r requirements-rag.txt; fi

# 再拷应用代码(.dockerignore 已挡住真实数据/密钥/venv)。
COPY . .

# SQLite 库与备份都写在容器内这个目录,compose 用命名卷挂到这里持久化。
ENV STOCKBOOK_DATABASE_URL=sqlite:////data/stockbook.db
VOLUME ["/data"]

EXPOSE 8000

# 0.0.0.0 才能从容器外访问;首启 init_db() 自动建库 + seed 示例策略。
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
