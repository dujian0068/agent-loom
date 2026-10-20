FROM python:3.14-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 LANGSMITH_TRACING=false LANGGRAPH_STRICT_MSGPACK=true
COPY api/requirements.lock ./requirements.lock
RUN pip install --no-cache-dir -r requirements.lock
COPY api/ ./
RUN pip install --no-deps -e . \
    && useradd --uid 10001 --create-home platform \
    && mkdir -p /var/lib/agentloom && chown platform:platform /var/lib/agentloom
USER platform
ENV PLATFORM_DATA_DIR=/var/lib/agentloom
EXPOSE 8000
CMD ["agentloom-platform", "api", "--host", "0.0.0.0"]
