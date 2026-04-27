# sre-aws-batch-telemetry — Makefile
#
# Available targets:
#   make build            — Build Docker image locally
#   make push             — Push image to ECR (requires prior 'build')
#   make build-and-push   — Build + authenticate + push
#   make deploy           — Deploy CloudFormation stacks
#   make submit-job       — Submit a test Batch job
#   make test             — Run Python unit tests
#   make clean            — Tear down all AWS resources

.PHONY: build push build-and-push deploy submit-job test clean help

# Configurable variables (override on CLI: make deploy ENV=staging)
ENV            ?= dev
AWS_REGION     ?= us-east-1
AWS_ACCOUNT_ID ?= $(shell aws sts get-caller-identity --query Account --output text 2>/dev/null)
SERVICE_NAME   := sre-aws-batch-telemetry
IMAGE_TAG      ?= latest
TEMPLATES_BUCKET ?=
VPC_ID         ?=
SUBNET_IDS     ?=
CONTAINER_IMAGE ?= $(AWS_ACCOUNT_ID).dkr.ecr.$(AWS_REGION).amazonaws.com/$(SERVICE_NAME):$(IMAGE_TAG)

# Python test runner
PYTHON ?= python3

help: ## Show this help message
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'

build: ## Build Docker image locally (tag: SERVICE_NAME:IMAGE_TAG)
	docker build \
		--file docker/Dockerfile \
		--tag $(SERVICE_NAME):$(IMAGE_TAG) \
		.

push: ## Push image to ECR (ECR repo must exist; run build first)
	@if [ -z "$(AWS_ACCOUNT_ID)" ]; then echo "ERROR: AWS_ACCOUNT_ID not set"; exit 1; fi
	aws ecr get-login-password --region $(AWS_REGION) | \
		docker login --username AWS --password-stdin \
		$(AWS_ACCOUNT_ID).dkr.ecr.$(AWS_REGION).amazonaws.com
	docker tag $(SERVICE_NAME):$(IMAGE_TAG) $(CONTAINER_IMAGE)
	docker push $(CONTAINER_IMAGE)

build-and-push: ## Build Docker image and push to ECR
	bash scripts/build-and-push.sh \
		--tag $(IMAGE_TAG) \
		--region $(AWS_REGION)

deploy: ## Deploy CloudFormation stacks (requires VPC_ID, SUBNET_IDS, TEMPLATES_BUCKET)
	@if [ -z "$(VPC_ID)" ]; then echo "ERROR: VPC_ID is required (e.g. make deploy VPC_ID=vpc-xxx)"; exit 1; fi
	@if [ -z "$(SUBNET_IDS)" ]; then echo "ERROR: SUBNET_IDS is required (e.g. make deploy SUBNET_IDS=subnet-a,subnet-b)"; exit 1; fi
	@if [ -z "$(TEMPLATES_BUCKET)" ]; then echo "ERROR: TEMPLATES_BUCKET is required"; exit 1; fi
	bash scripts/deploy.sh \
		--env $(ENV) \
		--vpc-id $(VPC_ID) \
		--subnet-ids "$(SUBNET_IDS)" \
		--container-image "$(CONTAINER_IMAGE)" \
		--templates-bucket $(TEMPLATES_BUCKET) \
		--region $(AWS_REGION)

submit-job: ## Submit a test AWS Batch job
	bash scripts/submit-job.sh \
		--env $(ENV) \
		--region $(AWS_REGION)

test: ## Run Python unit tests
	$(PYTHON) -m pytest tests/ -v --tb=short

clean: ## Tear down all AWS resources (CloudFormation stack + ECR images)
	bash scripts/cleanup.sh \
		--env $(ENV) \
		--region $(AWS_REGION)
