-- Create feedback table in Supabase
-- Run in SQL Editor: https://supabase.com/dashboard/project/tiviipaamwvfjvoscnyk/sql

CREATE TABLE IF NOT EXISTS feedback (
  id         uuid DEFAULT gen_random_uuid() PRIMARY KEY,
  user_id    uuid REFERENCES auth.users(id) ON DELETE SET NULL,
  rating     int CHECK (rating >= 1 AND rating <= 5),
  message    text,
  page       text,
  created_at timestamptz DEFAULT now()
);

-- Enable RLS
ALTER TABLE feedback ENABLE ROW LEVEL SECURITY;

-- Users can insert their own feedback
CREATE POLICY "Users can insert feedback"
ON feedback FOR INSERT
WITH CHECK (auth.uid() = user_id OR user_id IS NULL);

-- Users can view their own feedback
CREATE POLICY "Users can view own feedback"
ON feedback FOR SELECT
USING (auth.uid() = user_id);

-- Verify
SELECT 'feedback table created' as status;
