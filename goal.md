# 목표
* 'mp3 파일' 하나와 '기준 bpm' 값을 받는다
* '기준 bpm'에 맞는 드럼의 kick 으로 이루어진 mp3 생성
* 'mp3 파일'의 bpm을 측정한다.
* 'mp3 파일'의 bpm 싱크를 '기준 bpm'에 맞춘다
    * 'mp3 파일'의 원본 bpm 검출 (beat tracking)
    * 첫 비트(다운비트) 위치 검출
    * 템포 비율 계산 (기준 bpm / 원본 bpm)
    * 피치 유지하며 타임 스트레칭 (time-stretch)
    * 첫 비트를 '기준 bpm' kick 의 첫 박에 정렬
    * 결과 mp3 저장
* 검증용 mp3 생성
    * 왼쪽에는 kick mp3
    * 오른쪽에는 결과 mp3