# ORM 실행 가능 예제 모음

각 파일은 독립적으로 실행 가능합니다. (SQLite 인메모리 DB 사용, 별도 DB 서버 불필요)

## 실행 방법

```bash
# 1. SQLAlchemy
pip install sqlalchemy --break-system-packages
python 1_sqlalchemy_example.py

# 2. Django ORM (standalone)
pip install django --break-system-packages
python 2_django_orm_example.py

# 3. Peewee
pip install peewee --break-system-packages
python 3_peewee_example.py

# 4. Tortoise ORM (async)
pip install tortoise-orm --break-system-packages
python 4_tortoise_orm_example.py

# 5. SQLModel
pip install sqlmodel --break-system-packages
python 5_sqlmodel_example.py

# 6. Pony ORM
pip install pony --break-system-packages
python 6_pony_orm_example.py
```

## 공통 시나리오

모든 예제는 동일한 쿼리를 재현합니다:

> `users` 테이블에서 `age > 20`인 사용자의 `name`, `age` 컬럼만 조회

각 파일 안에서 같은 결과를 내는 여러 코드 스타일(컬럼 직접 지정 / values() / only() / raw SQL 우회 경로 등)을 순서대로 실행하고 출력합니다.

## 참고

- 실행 환경에 네트워크가 없어 이 자리에서 직접 pip install 및 실행 테스트는 하지 못했습니다.
  로컬 환경에서 위 명령대로 실행해 결과를 확인해 주세요.
- 각 파일에 있는 "raw SQL 직접 실행" 부분이 이전 대화에서 언급한 "ORM 정책 훅을 우회하는 경로"에 해당합니다.