# 토큰 허니팟 탐지기

Brownie 기반 동적 허니팟 탐지 도구입니다. 메인넷 포크 환경에서 토큰의 매수/매도를 시뮬레이션하여 허니팟 여부를 판단합니다.

## 설치

```bash
pip install -r requirements.txt
```

## 설정

`.env` 파일에 mainnet RPC API와 검사할 토큰 주소를 입력합니다:

```bash
# Ethereum Mainnet RPC API Key
WEB3_INFURA_PROJECT_ID=your_infura_project_id_here

# 검사할 토큰 정보
TOKEN_ADDRESS=0x6982508145454Ce325dDbE47a25d4ec3d2311933
```

**설정 항목:**
- `WEB3_INFURA_PROJECT_ID`: Infura API 키 (무료: https://infura.io)
- `TOKEN_ADDRESS`: 검사할 토큰 컨트랙트 주소


## 실행

```bash
python scripts/scam_analyzer.py {block_number} {router_name} [pair_creator]
```
**Arguments**
- `block_number`: 포크할 블록 번호 (유동성 풀이 생성된 시점)
- `router_name` : 토큰이 사용하는 라우터 이름
- `pair_creator` : 토큰 유동성 풀 배포자 주소 (Optional)(0x...)

**Available router list**  
아래에 명시된 라우터 이름만 인자로 사용할 수 있습니다:
- UniswapV2
- UniswapV2_Old
- SushiswapV2
- ShibaswapV1
- PancakeV2
- FraxswapV2

## 결과

테스트 결과는 `results/` 폴더에 JSON 형식으로 저장됩니다.

### 판정 기준

- **SAFE (정상)**: 실수령률 90% 이상
- **WARNING (주의)**: 실수령률 50~90%
- **HONEYPOT (의심)**: 실수령률 50% 미만 또는 매도 불가

## 프로젝트 구조

```
.
├── .env                     # RPC API 키 및 토큰 설정
├── brownie-config.yaml      # Brownie 네트워크 설정
├── scripts/
│   └── scam_analyzer.py # 메인 스크립트
├── interfaces/              # Uniswap/ERC20 인터페이스
└── results/                 # 테스트 결과 저장 폴더
```
