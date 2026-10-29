FROM python:3.14-slim
# Installing dependencies as root is intentional inside this image.
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_ROOT_USER_ACTION=ignore
WORKDIR /app
COPY requirements.txt pyproject.toml LICENSE THIRD_PARTY_LICENSES ./
RUN pip install -r requirements.txt
COPY src ./src
RUN pip install --no-deps .
RUN mkdir -p /app/tmp && chmod 1777 /app/tmp
ENTRYPOINT ["jmstats"]
CMD ["update"]
