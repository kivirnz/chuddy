#!/bin/bash

docker compose up --build -d && docker compose logs --tail 50 -f
