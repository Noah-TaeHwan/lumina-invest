from pydantic_settings import BaseSettings
import os


class Settings(BaseSettings):
    PORT: int = 8000
    SESSION_SECRET: str = "change-me-super-secret"
    SESSION_TTL: int = 604800  # 7 days

    REDIS_URL: str = "redis://localhost:6379"

    # ── PostgreSQL (SQLAlchemy async + asyncpg) ───────────────────────────────
    DATABASE_URL: str = "postgresql+asyncpg://lumina:lumina@localhost:5432/lumina"

    # ── JWT ──────────────────────────────────────────────────────────────────
    JWT_SECRET: str = "change-me-jwt-secret-32chars-min!!"
    JWT_ALGORITHM: str = "HS256"
    JWT_ACCESS_TTL: int = 900       # 15분 (초)
    JWT_REFRESH_TTL: int = 604800   # 7일 (초)

    OLLAMA_BASE_URL: str = "http://127.0.0.1:11434"
    LLM_MODEL: str = "llama3.1"
    EMBED_MODEL: str = "nomic-embed-text"
    VLM_MODEL: str = "llava"          # Vision-Language Model for image/slide description
    OLLAMA_TIMEOUT: float = 300.0

    # ── LLM 서빙 백엔드 선택 (채팅/에이전트 전용, 임베딩은 항상 Ollama 사용) ─────
    # ollama(기본, 로컬/EC2 Ollama) | bedrock | sagemaker | vllm
    LLM_PROVIDER: str = "ollama"

    # Bedrock (converse API 사용, AWS_REGION 재사용)
    BEDROCK_MODEL_ID: str = ""  # 예: us-east-1의 meta.llama3-1-8b-instruct-v1:0

    # SageMaker JumpStart 엔드포인트 (invoke_endpoint)
    SAGEMAKER_ENDPOINT_NAME: str = ""

    # EC2/ECS 위의 vLLM (OpenAI 호환 /v1/chat/completions)
    VLLM_BASE_URL: str = ""
    VLLM_MODEL: str = ""

    VECTOR_STORE: str = "qdrant"
    QDRANT_URL: str = "http://localhost:6333"
    QDRANT_COLLECTION: str = "fin_chunks"
    DOCUMENT_COLLECTION: str = "fin_chunks"  # Qdrant collection for uploaded documents

    DATA_DIR: str = "./data"
    TOP_K: int = 6

    # ── 근거 판정 채팅(A-2) ──────────────────────────────────────────────────
    # 끄면 /api/evidence/*·/api/conversations/{cid}/evidence가 404다. 문단 적재와 TypeSafe 키가 있는 로컬에서만 켠다.
    EVIDENCE_CHAT_ENABLED: bool = False
    # 근거 모드 답변 생성기. 앱 기본 LLM_MODEL과 분리한다(임계값이 이 생성기에 묶인다, spec 결정 4-2)
    EVIDENCE_LLM_MODEL: str = "llama3.1:8b"
    # 일일 JEV 한도(호출 수·입력 토큰 수, KST 자정에 풀림, spec 7.2절)
    EVIDENCE_DAILY_USER_CALLS: int = 150
    EVIDENCE_DAILY_USER_TOKENS: int = 750_000
    EVIDENCE_DAILY_GLOBAL_TOKENS: int = 3_000_000
    # ── 투자 판단 일지(모듈 C) ───────────────────────────────────────────────
    # 끄면 /api/journal 전체가 404이고, 판정 API에서는 일지 표(0010)를 조회하지 않는다. 근거 모드 플래그와 묶지 않는다
    # (근거 모드를 꺼도 자기 기록을 보고·내보내고·지울 수 있어야 한다, spec 결정 5-6).
    # 관리자 stats·reset은 플래그와 무관하게 일지 표를 돈다 — 0010 적용 필수
    JOURNAL_ENABLED: bool = False

    NEO4J_URI: str = "bolt://localhost:7687"
    NEO4J_USER: str = "neo4j"
    NEO4J_PASSWORD: str = "finagent123"

    # ── AWS (Comprehend 감성분석 / SageMaker 배치 학습 결과 조회) ──────────────
    AWS_REGION: str = "ap-northeast-2"  # 실제 운영 EC2(fund-web)가 있는 리전
    ML_ARTIFACTS_BUCKET: str = ""  # SageMaker 학습 산출물(scores.json)이 저장된 S3 버킷

    # ── QuantConnect LEAN 백테스트 (domain-rag-lab / stock-coin-trade 이식) ──────
    # auto | ssh | docker | local  (auto: SSH 설정 있으면 ssh → docker CLI 있으면 docker → local)
    LEAN_MODE: str = "auto"
    LEAN_DOCKER_IMAGE: str = "quantconnect/lean:latest"
    LEAN_TIMEOUT_SECONDS: int = 300
    LEAN_KEEP_WORKDIR: bool = False           # 디버깅용: 실행 폴더(main.py/prices.csv/results) 보존
    # docker 모드: 이 프로세스가 쓰는 작업 폴더. 컨테이너 안에서 실행하면 같은 경로가 호스트에서
    # LEAN_HOST_WORKDIR로 보이도록 volume을 맞춰야 한다 (docker-compose.yml 참고).
    LEAN_WORKDIR: str = "./data/lean-workflows"
    LEAN_HOST_WORKDIR: str = ""
    # docker 모드에서 LEAN_WORKDIR 대신 named volume 이름을 마운트 (컨테이너 안에서 실행할 때 권장)
    LEAN_DOCKER_VOLUME: str = ""
    DOCKER_SOCK: str = "/var/run/docker.sock"
    # ssh 모드: 원격 LEAN 실행 서버 (PEM 키는 저장소에 넣지 말고 읽기 전용으로 마운트)
    LEAN_SSH_HOST: str = ""
    LEAN_SSH_USER: str = "ubuntu"
    LEAN_SSH_KEY_PATH: str = ""
    LEAN_REMOTE_WORKDIR: str = "/home/ubuntu/lean-workflows"

    # ── Alpaca Paper Trading (읽기 전용 연결 테스트 + 퀀트 파이프라인 주문) ─────
    ALPACA_API_KEY: str = ""
    ALPACA_SECRET_KEY: str = ""

    # ── 외부 Open API (/openapi/v1) 호출 제한 ────────────────────────────────
    OPENAPI_RATE_LIMIT_MAX: int = 60       # 키당 분당 호출 수
    OPENAPI_RATE_LIMIT_WINDOW: int = 60    # 초

    # ── TradingView Webhook 보호 ─────────────────────────────────────────────
    # TradingView 공식 알림 발신 IP (docs: Webhooks). 운영에서 TRADINGVIEW_ENFORCE_IP=true 로 켠다.
    TRADINGVIEW_ALLOWED_IPS: str = "52.89.214.238,34.212.75.30,54.218.53.128,52.32.178.7"
    TRADINGVIEW_ENFORCE_IP: bool = False
    TRADINGVIEW_RATE_LIMIT_MAX: int = 30    # 키당 분당 알림 수

    # ── 운영 ────────────────────────────────────────────────────────────────
    # 앱 기동 시 alembic upgrade head 실행 여부. 복제 인스턴스가 여러 개인 운영 환경에서는 false 로 두고
    # 배포 단계에서 scripts/migrate.sh 로 1회 실행한다.
    RUN_MIGRATIONS_ON_STARTUP: bool = True
    # 로컬 기능 확인 시 대량 시세 수집을 끈다. 기존 실행의 기본 동작은 유지한다.
    STARTUP_DATA_SYNC_ENABLED: bool = True
    PUBLIC_BASE_URL: str = ""               # Webhook URL 안내 등에 쓰는 외부 공개 주소 (예: https://fund.example.com)

    ADMIN_EMAILS: str = ""
    TRUST_PROXY: bool = False
    COOKIE_SECURE: bool = False
    COOKIE_SAMESITE: str = "lax"

    # ── 알림 채널 설정 ──────────────────────────────────────────────────────────

    # 텔레그램
    TELEGRAM_BOT_TOKEN: str = ""
    TELEGRAM_CHAT_ID: str = ""

    # Slack Incoming Webhook
    SLACK_WEBHOOK_URL: str = ""

    # 이메일 (SMTP / STARTTLS)
    SMTP_HOST: str = ""
    SMTP_PORT: int = 587
    SMTP_USER: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_FROM: str = ""
    SMTP_TO: str = ""

    # 카카오 알림톡 · SMS (CoolSMS REST API)
    COOLSMS_API_KEY: str = ""
    COOLSMS_API_SECRET: str = ""
    KAKAO_SENDER_KEY: str = ""   # 카카오 채널 발신 프로필 키
    KAKAO_PHONE: str = ""        # 수신 전화번호 (예: 01012345678)
    SMS_FROM: str = ""           # 발신 번호
    SMS_TO: str = ""             # 수신 번호

    class Config:
        env_file = os.getenv("ENV_FILE", ".env.dev")
        extra = "ignore"

    @property
    def admin_email_list(self) -> list[str]:
        return [e.strip() for e in self.ADMIN_EMAILS.split(",") if e.strip()]


settings = Settings()
