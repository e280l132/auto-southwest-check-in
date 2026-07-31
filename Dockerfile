# Debian rather than Alpine because Google publishes Chrome only for glibc. Alpine's `chromium`
# package is a distro rebuild with a different fingerprint, which is a suspect in Southwest's
# rejection rate; this image runs Google's own binary instead.
#
# Chrome is amd64-only, so this image is amd64-only. Build with --platform=linux/amd64 from an
# arm64 machine (note Chrome does not run under emulation -- build there, run on a real amd64 host).
FROM python:3.14-slim-bookworm

WORKDIR /app

# Define so the script knows not to download a new driver version, as
# this Docker image already downloads a compatible chromedriver
ENV AUTO_SOUTHWEST_CHECK_IN_DOCKER=1

RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl gnupg tini unzip wget xauth xvfb \
  && wget -qO /tmp/google.pub https://dl.google.com/linux/linux_signing_key.pub \
  && gpg --dearmor -o /usr/share/keyrings/google-chrome.gpg /tmp/google.pub \
  && echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-chrome.gpg] https://dl.google.com/linux/chrome/deb/ stable main" \
       > /etc/apt/sources.list.d/google-chrome.list \
  && apt-get update && apt-get install -y --no-install-recommends google-chrome-stable \
  && rm -rf /var/lib/apt/lists/* /tmp/google.pub

# Fetch the chromedriver matching whatever Chrome version just landed. Pinning Chrome instead
# would break rebuilds, as Google drops older versions from the apt repo.
RUN set -eux; \
    CHROME_VERSION="$(google-chrome --version | awk '{print $3}')"; \
    CHROME_MAJOR="${CHROME_VERSION%%.*}"; \
    DRIVER_URL="$(curl -fsS https://googlechromelabs.github.io/chrome-for-testing/known-good-versions-with-downloads.json \
      | python3 -c "import json,sys; \
versions=[v for v in json.load(sys.stdin)['versions'] if v['version'].split('.')[0]=='$CHROME_MAJOR']; \
print([d['url'] for d in versions[-1]['downloads']['chromedriver'] if d['platform']=='linux64'][0])")"; \
    echo "Chrome ${CHROME_VERSION}, chromedriver from ${DRIVER_URL}"; \
    wget -qO /tmp/chromedriver.zip "$DRIVER_URL"; \
    unzip -qj /tmp/chromedriver.zip '*/chromedriver' -d /usr/local/bin; \
    chmod +x /usr/local/bin/chromedriver; \
    rm -f /tmp/chromedriver.zip; \
    chromedriver --version

RUN pip install --no-cache-dir uv

RUN useradd --create-home --home-dir /app --shell /bin/bash auto-southwest-check-in
RUN chown -R auto-southwest-check-in:auto-southwest-check-in /app
RUN mkdir -p /app/data && chown auto-southwest-check-in:auto-southwest-check-in /app/data
USER auto-southwest-check-in

COPY requirements.txt ./
RUN uv venv /app/.venv \
  && uv pip install --python /app/.venv/bin/python --no-cache -r requirements.txt
# Make sure the Python virtual environment is used
ENV PATH="/app/.venv/bin:$PATH"

# The webdriver is started with driver_version="keep" in Docker, so seleniumbase must already
# have a driver in place rather than downloading one that matches a browser it guessed at
RUN SB_DRIVERS="$(/app/.venv/bin/python3 -c 'import os, seleniumbase; print(os.path.join(os.path.dirname(seleniumbase.__file__), "drivers"))')" \
  && mkdir -p "$SB_DRIVERS" \
  && install -m 0755 /usr/local/bin/chromedriver "$SB_DRIVERS/uc_driver" \
  && install -m 0755 /usr/local/bin/chromedriver "$SB_DRIVERS/chromedriver"

# Point the app at Google's Chrome rather than letting seleniumbase discover another browser
ENV AUTO_SOUTHWEST_CHECK_IN_BROWSER_PATH=/usr/bin/google-chrome

COPY . .

# Use tini so Selenium zombie processes exit cleanly
ENTRYPOINT ["tini", "--", "python3", "-u", "southwest.py"]
