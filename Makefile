# Distributed tracing harness. Typical flow:
#   make install services-up all matrix services-down
# Each case-* target runs the case, then verifies it in LangSmith.

VERIFY := uv run python verify.py

CASES_1 := 1a 1b 1c 1d
CASES_2 := 2a 2b 2c
CASES_3 := 3a 3b
CASES_4 := 4a 4b 4c 4d 4e
CASES_6 := 6a 6b 6c 6d
ALL := $(CASES_1) $(CASES_2) $(CASES_3) $(CASES_4) 5 $(CASES_6) 7

.PHONY: install services-up services-down services-status all matrix clean-results \
        case-1 case-2 case-3 case-4 case-6

install:
	@for d in . parent_langgraph child_langgraph mcp_server adk_service strands_service mda_agent; do \
		echo "uv sync $$d"; (cd $$d && uv sync -q) || exit 1; done

services-up:
	@scripts/services.sh up
services-down:
	@scripts/services.sh down
services-status:
	@scripts/services.sh status

# Group targets, matching the case numbers in the README.
case-1: $(addprefix case-,$(CASES_1))
case-2: $(addprefix case-,$(CASES_2))
case-3: $(addprefix case-,$(CASES_3))
case-4: $(addprefix case-,$(CASES_4))
case-6: $(addprefix case-,$(CASES_6))

all: $(addprefix case-,$(ALL))

# 4b and 4c switch the parent's LangSmith client to OTel export.
case-4b: MODE := hybrid
case-4c: MODE := otel

# The runner never fails the build: errors are recorded and reported by verify.py.
case-%:
	@cd parent_langgraph && $(if $(MODE),LANGSMITH_TRACING_MODE=$(MODE)) uv run python cases.py $* || true
	@$(VERIFY) $*

matrix:
	@$(VERIFY) --matrix

clean-results:
	rm -rf results/runs results/verify results/matrix.md
