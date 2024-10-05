FROM python:3.12-slim
WORKDIR /
ADD requirements.txt requirements.txt
RUN pip3 install -r requirements.txt

RUN apt-get update                             \
 && apt-get install -y --no-install-recommends \
    ca-certificates curl firefox-esr  \
 && echo ttf-mscorefonts-installer msttcorefonts/accepted-mscorefonts-eula select true | debconf-set-selections \              \
 && echo "deb http://deb.debian.org/debian bookworm contrib non-free" > /etc/apt/sources.list.d/contrib.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends ttf-mscorefonts-installer \
 && curl -L https://github.com/mozilla/geckodriver/releases/download/v0.33.0/geckodriver-v0.33.0-linux64.tar.gz | tar xz -C /usr/local/bin

COPY . .
ENTRYPOINT ["python", "spinnyBoi.py"]