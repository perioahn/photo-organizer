# 사진 정리 (배포판)

치과 임상사진 정리 앱 — 브라우저 기반, 완전 로컬 동작 (클라우드 전송 없음).

## 이 배포판에 포함된 기능
- 환자/날짜 폴더 브라우저: 즉시 검색, 썸네일, 이미지 뷰어(RAW 지원), 최근 촬영순 정렬
- 태그 편집: 카테고리별 태그 피커, 폴더명 자동 정규 순서 기록, 되돌리기(저널)
- **새 사진 추가 마법사**: 카메라 폴더 선택 → 정보사진/임상사진 자동 분류(EXIF 조리개)
  → 진료번호 로컬 OCR(RapidOCR) → 검토·수정 후 복사. 원본 불변, 되돌리기 가능
- 스케줄 PDF 검색(선택): PDF 폴더 지정 시 환자번호로 즉시 검색·열기

## 본판과의 차이 (제거된 것)
- Google Vision OCR 폴백 없음 — 로컬 OCR 전용 (자격증명·클라우드 불필요)
- 차트 기반 자동 태깅(리뷰 큐/차트 스윕) 없음 — 태그는 수동 편집

## 설치 A — 실행파일 (Python 불필요)
[Releases](../../releases)에서 다운로드:
- Windows: `PhotoApp-windows.zip` 압축 해제 → `PhotoApp\PhotoApp.exe` 실행
- macOS: `PhotoApp-macos.zip` 압축 해제 → `PhotoApp.app` 실행
  (서명되지 않은 앱이라 첫 실행은 **우클릭 → 열기**, 또는 터미널에서
  `xattr -d com.apple.quarantine PhotoApp.app`)

## 설치 B — 소스 실행 (Windows)
1. Python 3.13 설치 (python.org, "Add to PATH" 체크)
2. 이 폴더에서:
   ```
   py -3.13 -m pip install -r requirements.txt
   cd frontend && npm install && npm run build && cd ..
   ```
   (Node.js가 없으면 본판 PC에서 만든 frontend/dist 폴더를 복사해 넣어도 됨)
3. 실행: `실행.bat` 더블클릭 (또는 `py -3.13 run.py`)

## 첫 실행
- 브라우저가 자동으로 열리고 "처음 설정 — 폴더 지정" 화면에서 사진 폴더를 선택
- 사진 폴더 구조: `루트\{진료번호8자리}_{이름}\{YYMMDD}_{태그...}\사진들`
- 선택한 폴더는 저장되어 다음 실행부터 자동 적용 (⚙ 폴더 설정에서 변경)
- 브라우저 탭을 모두 닫으면 30초 후 앱이 자동 종료됩니다

## 데이터 위치
- 사진: 지정한 폴더 (폴더명이 곧 데이터 — 앱 없이도 탐색기로 열람 가능)
- 인덱스/썸네일 캐시/저널: `%LOCALAPPDATA%\PhotoApp` (지워도 자동 재생성, 저널만 백업 권장)
