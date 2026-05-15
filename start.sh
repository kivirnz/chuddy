#!/bin/bash

docker compose up --build -d -t 0 && docker compose logs --tail 100 -f
