# Outputs

실행별 최종 보고서를 `outputs/<run_id>.md`에 저장합니다.

중간 Worker 결과는 부모 State의 `results[].output`에 dict로 누적합니다.
`report_uri`는 최종 보고서 또는 작성 실패 시 부분 결과 보고서의 경로입니다.
