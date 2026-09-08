interface SearchInputProps {
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
  testId?: string;
}

export default function SearchInput({
  value,
  onChange,
  placeholder = "Search",
  testId = "search-input",
}: SearchInputProps) {
  return (
    <input
      type="search"
      className="search-input"
      placeholder={placeholder}
      value={value}
      onChange={(event) => onChange(event.target.value)}
      data-testid={testId}
    />
  );
}
