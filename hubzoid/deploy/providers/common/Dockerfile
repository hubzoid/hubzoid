# This hub as an image: the released Hubzoid image with the hub's files.
# Written by `hubzoid deploy init {{provider}}` (Hubzoid {{version}}).
# The hub's files come from Git and are only read; everything the hub writes
# goes to the durable volume at /data. The folder keeps the hub's name:
# access grants, secret references and backups name the hub by it.
FROM {{image}}
COPY . /hubs/{{hub}}
WORKDIR /hubs/{{hub}}
ENV HUBZOID_DATA_DIR=/data
CMD ["run", "/hubs/{{hub}}"]
