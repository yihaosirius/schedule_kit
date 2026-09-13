-- 上课地点属于「某次课」而不是「这门课」：同一门课换教室是常态。
-- 因此把 location 下沉到 course_sessions，courses.location 退化为默认值。

ALTER TABLE course_sessions ADD COLUMN location TEXT NOT NULL DEFAULT '';

CREATE INDEX idx_sessions_weekday ON course_sessions(weekday);
