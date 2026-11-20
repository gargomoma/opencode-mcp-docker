IMAGE := opencode-mcp:latest
COMPOSE := docker compose

.PHONY: up build update version down logs sh test clean

up:            ## build if needed and start
	$(COMPOSE) up -d --build

build:         ## rebuild the image only (cached layers)
	$(COMPOSE) build

update:        ## re-install the latest opencode CLI, then restart
	$(COMPOSE) build --build-arg OPENCODE_REFRESH=$$(date +%s)
	$(COMPOSE) up -d

version:       ## which opencode is inside the running container
	$(COMPOSE) exec opencode opencode --version

down:          ## stop and remove the container
	$(COMPOSE) down

logs:          ## follow both processes' output
	$(COMPOSE) logs -f --tail=50

sh:            ## shell inside the running container
	$(COMPOSE) exec opencode bash

test:          ## 401 without the password, 200 with it
	@. ./.env && curl -s -o /dev/null -w 'no auth: %{http_code}\n' -X POST http://127.0.0.1:8123/mcp \
	  -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
	  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"c","version":"0"}}}'
	@. ./.env && curl -s -o /dev/null -w 'bearer : %{http_code}\n' -X POST http://127.0.0.1:8123/mcp \
	  -H "Authorization: Bearer $$OPENCODE_SERVER_PASSWORD" \
	  -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
	  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"c","version":"0"}}}'

clean:         ## image + volumes (destroys opencode state)
	$(COMPOSE) down -v --rmi local
