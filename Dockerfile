FROM python:3.11-bookworm

# Install ffmpeg (bookworm includes zscale via libzimg2 for HDR tone mapping, and
# its ffmpeg build already ships the QSV/VAAPI/NVENC encoders).
#
# VA-API drivers are baked in so one image covers Intel (iHD, plus i965 for
# pre-Gen8) and AMD (mesa) hardware transcoding. They stay inert unless a GPU
# device is passed into the container, so GPU-less deployments are unaffected.
# NVENC needs no in-image driver — nvidia-container-toolkit injects it from the host.
# contrib/non-free is enabled because Intel's full iHD driver
# (intel-media-va-driver-non-free) is not in main.
RUN sed -i 's/^Components: main$/Components: main contrib non-free non-free-firmware/' \
        /etc/apt/sources.list.d/debian.sources && \
    apt-get update && \
    apt-get install -y --no-install-recommends \
        ffmpeg \
        libzimg2 \
        intel-media-va-driver-non-free \
        i965-va-driver \
        mesa-va-drivers \
        vainfo && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies first (better layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Bring in the rest of the project
COPY . .

# Install core package (editable)
RUN pip install --no-cache-dir -e .

# Make entrypoint executable
RUN chmod +x entrypoint.sh

CMD ["./entrypoint.sh"]
