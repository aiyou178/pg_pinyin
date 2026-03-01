-- Usage:
-- psql \\
--   -v mapping_file='/absolute/path/pinyin_mapping.csv' \\
--   -v token_file='/absolute/path/pinyin_token.csv' \\
--   [-v words_file='/absolute/path/pinyin_words.csv'] \\
--   -f sql/load_data.sql

\\if :{?mapping_file}
\\else
\\echo 'missing required variable: mapping_file'
\\quit 1
\\endif

\\if :{?token_file}
\\else
\\echo 'missing required variable: token_file'
\\quit 1
\\endif

TRUNCATE TABLE pinyin.pinyin_mapping;
TRUNCATE TABLE pinyin.pinyin_token;
TRUNCATE TABLE pinyin.pinyin_words;

COPY pinyin.pinyin_mapping (character, pinyin)
FROM :'mapping_file'
WITH (FORMAT csv, HEADER false, DELIMITER ',');

COPY pinyin.pinyin_token (character, category)
FROM :'token_file'
WITH (FORMAT csv, HEADER false, DELIMITER ',');

\\if :{?words_file}
COPY pinyin.pinyin_words (word, pinyin)
FROM :'words_file'
WITH (FORMAT csv, HEADER false, DELIMITER ',');
\\else
\\echo 'words_file not provided, pinyin_words stays empty'
\\endif

INSERT INTO pinyin.pinyin_mapping (character, pinyin)
VALUES (' ', ' ')
ON CONFLICT (character) DO NOTHING;
