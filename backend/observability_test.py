import requests
import json
from datetime import datetime, timedelta, timezone  # timezone 추가

# 각 데이터소스의 로컬 API 주소
PROMETHEUS_URL = "http://localhost:9090/api/v1/query"
LOKI_URL = "http://localhost:3100/loki/api/v1/query_range"

def query_prometheus(promql: str) -> dict:
    """Prometheus에 PromQL을 실행하여 메트릭을 조회합니다."""
    print(f"[메트릭 조회] PromQL: {promql}")
    
    try:
        response = requests.get(PROMETHEUS_URL, params={"query": promql}, timeout=5)
        response.raise_for_status()
        data = response.json()
        return data.get("data", {}).get("result", [])
    except Exception as e:
        return {"error": str(e)}

def query_loki(logql: str, limit: int = 10, minutes_ago: int = 60) -> dict:
    """Loki에 LogQL을 실행하여 최근 로그를 조회합니다."""
    print(f"[로그 조회] LogQL: {logql} (최대 {limit}줄, 최근 {minutes_ago}분)")
    
    # timezone-aware UTC 시간 사용 (파이썬 3.12+ 권장 방식)
    end_time = datetime.now(timezone.utc)
    start_time = end_time - timedelta(minutes=minutes_ago)
    
    params = {
        "query": logql,
        "limit": limit,
        "start": str(int(start_time.timestamp() * 1e9)),  # 정확한 UTC 타임스탬프 (나노초)
        "end": str(int(end_time.timestamp() * 1e9))
    }
    
    try:
        response = requests.get(LOKI_URL, params=params, timeout=5)
        response.raise_for_status()
        data = response.json()
        
        results = data.get("data", {}).get("result", [])
        extracted_logs = []
        
        for stream in results:
            for val in stream.get("values", []):
                extracted_logs.append(val[1])
                
        return extracted_logs
    except Exception as e:
        return {"error": str(e)}

if __name__ == "__main__":
    print("=== 1. Prometheus 테스트 ===")
    prom_result = query_prometheus("up")
    print(json.dumps(prom_result, indent=2, ensure_ascii=False))
    
    print("\n=== 2. Loki 테스트 ===")
    # Loki 로그 조회를 위한 범용 LogQL
    loki_result = query_loki('{job=~".+"}', limit=5)
    print(json.dumps(loki_result, indent=2, ensure_ascii=False))