use std::io::{self, BufRead};

use lindera::dictionary::{DictionaryKind, load_embedded_dictionary};
use lindera::mode::Mode;
use lindera::segmenter::Segmenter;
use lindera::tokenizer::Tokenizer;

fn detail_at<'a>(details: &'a [&str], index: usize) -> &'a str {
    details
        .get(index)
        .copied()
        .filter(|value| !value.is_empty())
        .unwrap_or("*")
}

fn validate_tsv_field(value: &str, field: &str) -> Result<(), Box<dyn std::error::Error>> {
    if value.is_empty() || value.contains(['\t', '\r', '\n']) {
        return Err(
            format!("{field} must be non-empty and contain no TSV control characters").into(),
        );
    }
    Ok(())
}

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let dictionary = load_embedded_dictionary(DictionaryKind::UniDic)?;
    let segmenter = Segmenter::new(Mode::Normal, dictionary, None);
    let tokenizer = Tokenizer::new(segmenter);

    for (line_offset, line) in io::stdin().lock().lines().enumerate() {
        let line_number = line_offset + 1;
        let line = line?;
        let (id, text) = line
            .split_once('\t')
            .ok_or_else(|| format!("stdin:{line_number}: expected id<TAB>text"))?;
        validate_tsv_field(id, "id")?;
        validate_tsv_field(text, "text")?;

        let mut tokens = tokenizer.tokenize(text)?;
        for (token_index, token) in tokens.iter_mut().enumerate() {
            let surface = token.surface.to_string();
            validate_tsv_field(&surface, "token surface")?;
            let byte_start = token.byte_start;
            let byte_end = token.byte_end;
            let details = token.details();
            let fields = [
                detail_at(&details, 0),
                detail_at(&details, 1),
                detail_at(&details, 2),
                detail_at(&details, 6),
                detail_at(&details, 8),
                detail_at(&details, 9),
            ];
            for field in fields {
                validate_tsv_field(field, "token detail")?;
            }
            println!(
                "T\t{id}\t{token_index}\t{byte_start}\t{byte_end}\t{surface}\t{}\t{}\t{}\t{}\t{}\t{}",
                fields[0], fields[1], fields[2], fields[3], fields[4], fields[5]
            );
        }
        println!("E\t{id}");
    }
    Ok(())
}
