FROM python:3.9

WORKDIR /
COPY . .
RUN pip3 install -r requirements.txt

ENTRYPOINT ["python", "spinnyBoi.py"]