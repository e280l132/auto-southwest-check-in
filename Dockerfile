FROM python:3.14-alpine3.22

WORKDIR /app

# Define so the script knows not to download a new driver version, as
# this Docker image already downloads a compatible chromedriver
ENV AUTO_SOUTHWEST_CHECK_IN_DOCKER=1

RUN apk add --update --no-cache chromium chromium-chromedriver xvfb xauth uv

RUN adduser -D auto-southwest-check-in -h /app
RUN chown -R auto-southwest-check-in:auto-southwest-check-in /app
USER auto-southwest-check-in

COPY requirements.txt ./
RUN uv venv /app/.venv \
  && uv pip install --python /app/.venv/bin/python --no-cache -r requirements.txt
# Make sure the Python virtual environment is used
ENV PATH="/app/.venv/bin:$PATH"

COPY . .

ENTRYPOINT ["python3", "-u", "southwest.py"]
